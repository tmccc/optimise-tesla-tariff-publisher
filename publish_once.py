"""Prepare or run one explicitly armed tariff test; no recurring publisher."""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from prepare import private_json
from publisher_state import plan_publish,verified_publish_records
from runtime import ROOT,FallbackAdapter,live_policy_enabled,private_document
from tariff_schema import semantic_hash
from writer import TariffWriter,validate_candidate,publisher_policy_enabled
from active_mode import read_active_mode
from config import STATE_DIR, task_env

OUTPUT=STATE_DIR/'output'
PUBLISHER_POLICY=STATE_DIR/'publisher-policy.json'


def optional_private(path):
    return private_document(path) if path.exists() else {}


def watchdog_health():
    health=optional_private(OUTPUT/'watchdog-health.json')
    pid=health.get('pid');alive=False
    if type(pid) is int and pid>0:
        try:os.kill(pid,0);alive=True
        except (ProcessLookupError,PermissionError):pass
    return {'continuous':health.get('continuous') is True,
        'armed':health.get('live_execution_enabled') is True,
        'last_cycle_at':health.get('cycle_completed_at'),'pid':pid,'pid_alive':alive}


def context(*,snapshot=None):
    report=private_document(OUTPUT/'readiness.json')
    source=private_document(OUTPUT/'guard-input.json')
    candidate=private_document(OUTPUT/'frozen_current_quote-proposal.json')['tou_settings']['tariff_content_v2']
    manifest=report['candidates']['frozen_current_quote']['manifest']
    ownership=optional_private(OUTPUT/'publisher-ownership.json')
    snapshot=snapshot or FallbackAdapter(ownership).read_snapshot()
    digest=semantic_hash(candidate)
    policy=private_document(PUBLISHER_POLICY)
    expected=policy.get('expected_installed_semantic_hash')
    site=policy.get('energy_site_id')
    enabled=(publisher_policy_enabled(PUBLISHER_POLICY,candidate_hash=digest,
        expected_site_id=site,expected_installed_hash=expected) and live_policy_enabled())
    state={**source,**snapshot,'expected_baseline_hash':expected,
        'candidate_tariff_hash':digest,'candidate_valid':not validate_candidate(candidate,manifest,now=datetime.now(timezone.utc)),
        'candidate_prices_verified':report.get('candidate_comparison',{}).get('all_exact') is True,
        'fallback_approved':policy.get('fallback_design_approved') is True,
        'frozen_assumption_approved':policy.get('unquoted_planning_assumption_approved') is True,
        'live_publication_enabled':enabled,'publisher_pid':os.getpid(),
        'watchdog':watchdog_health(),'ownership':ownership,
        'preserve_settings_before':snapshot.get('preserve_settings')}
    return state,candidate,manifest


def prepare_test():
    # Preparation may pin a candidate while unarmed; never disarm an existing
    # active run or change a live policy implicitly.
    policy=private_document(PUBLISHER_POLICY)
    if policy.get('live_execution_enabled') is True or policy.get('allow_tariff_writes') is True:
        raise ValueError('cannot_repin_an_armed_policy')
    snapshot=FallbackAdapter(optional_private(OUTPUT/'publisher-ownership.json')).read_snapshot()
    candidate=private_document(OUTPUT/'frozen_current_quote-proposal.json')['tou_settings']['tariff_content_v2']
    policy.update({'approved_candidate_semantic_hash':semantic_hash(candidate),
        'expected_installed_semantic_hash':snapshot.get('current_tariff_hash'),
        'energy_site_id':snapshot.get('energy_site_id'),'approval_id':str(uuid.uuid4())})
    private_json(PUBLISHER_POLICY,policy)
    state,candidate,manifest=context(snapshot=snapshot)
    plan=plan_publish(state,now=datetime.now(timezone.utc))
    prepared={'prepared_at':datetime.now(timezone.utc).isoformat(),'preparation_only':True,
        'tesla_control_requests':0,'live_execution_enabled':False,
        'approval_id':policy['approval_id'],
        'plan':plan,'candidate_semantic_hash':semantic_hash(candidate),
        'known_valid_from':manifest['known_valid_from'],'known_valid_until':manifest['known_valid_until'],
        'quoted_interval_count':manifest['quoted_interval_count'],
        'planning_assumption':manifest['unquoted_assumed_pair'],
        'initial_mode':state.get('mode'),'preserve_settings':state.get('preserve_settings'),
        'optimise_battery_enrolled':state.get('battery_enrolled'),
        'test_expiry_action':'Self-Powered after a lease of at most three minutes; watchdog may act on the next 60-second cycle.',
        'no_automatic_return_to_savings':True,
        'supervised_test':True,
        'unverified_initial_write_requires_manual_recovery':True,
        'automatic_fallback_requires_committed_verified_ownership_and_lease':True,
        'not_a_recurring_publisher':True}
    private_json(OUTPUT/'controlled-test-plan.json',prepared)
    return prepared


def apply_test(*,writer_factory=TariffWriter):
    # Refuse before credentials/network if the explicit one-shot policy is off.
    policy=private_document(PUBLISHER_POLICY)
    if policy.get('live_execution_enabled') is not True or policy.get('allow_tariff_writes') is not True:
        return {'status':'live_execution_disabled','control_requests':0}
    if not live_policy_enabled():
        return {'status':'fallback_execution_disabled','control_requests':0}
    fd=os.open(STATE_DIR/'.control.lock',os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        approval=policy.get('approval_id')
        try:
            if not isinstance(approval,str) or str(uuid.UUID(approval))!=approval:
                raise ValueError('approval_id_invalid')
        except (ValueError,AttributeError):
            return {'status':'approval_id_invalid','control_requests':0}
        previous=optional_private(OUTPUT/'controlled-test-receipt.json')
        if previous.get('approval_id')==approval and previous.get('status')!='attempt_prepared':
            return {'status':'approval_already_attempted_manual_review_required','control_requests':0}
        state,candidate,manifest=context()
        plan=plan_publish(state,now=datetime.now(timezone.utc))
        if plan.get('allow_write') is not True:
            return {'status':'preconditions_blocked','control_requests':0,'plan':plan}
        if plan.get('write_required') is not True:
            return {'status':'no_controlled_test_write_required','control_requests':0,'plan':plan}
        receipt={'status':'attempt_prepared','control_requests':0,
            'approval_id':approval,
            'created_at':datetime.now(timezone.utc).isoformat(),
            'candidate_semantic_hash':semantic_hash(candidate),
            'prior_semantic_hash':state['current_tariff_hash']}
        private_json(OUTPUT/'controlled-test-receipt.json',receipt)

        def final_authorizer(observations):
            fresh=dict(state)
            fresh.update(observations)
            active=read_active_mode()
            fresh['mode']=active['mode']
            fresh['watchdog']=watchdog_health()
            fresh['live_publication_enabled']=(publisher_policy_enabled(PUBLISHER_POLICY,
                candidate_hash=state['candidate_tariff_hash'],expected_site_id=state['energy_site_id'],
                expected_installed_hash=state['expected_baseline_hash']) and live_policy_enabled())
            if private_document(PUBLISHER_POLICY).get('approval_id')!=approval:return False
            return plan_publish(fresh,now=datetime.now(timezone.utc)).get('allow_write') is True

        writer=writer_factory(final_authorizer=final_authorizer)
        # Persist an ambiguous outcome if this process fails after entering the
        # sender. Never claim zero effects merely because a receipt write fails.
        receipt.update({'status':'dispatch_in_progress','control_requests':None})
        private_json(OUTPUT/'controlled-test-receipt.json',receipt)
        result=writer.publish(candidate,manifest=manifest,
            expected_site_id=state['energy_site_id'],
            expected_installed_hash=state['expected_baseline_hash'],
            preserve_settings=state['preserve_settings'])
        receipt['writer_result']=result
        receipt['control_requests']=1 if result.get('command_sent') is True else (
            0 if result.get('command_sent') is False else None)
        readback=result.get('readback') or {}
        if readback:
            active=read_active_mode()
            readback['cloud_default_mode']=readback.get('mode')
            readback['mode']=active['mode']
            readback['mode_source']=active['mode_source']
            readback['active_mode_observation']=active
            readback['observed_at']=datetime.now(timezone.utc).isoformat()
        proof={**state,'fresh_readback_tariff_hash':result.get('fresh_readback_tariff_hash'),
            'readback':readback,'write_acknowledged':result.get('write_acknowledged') is True,
            'write_acknowledged_at':result.get('write_acknowledged_at'),
            'preserve_settings_after':readback.get('preserve_settings')}
        records=verified_publish_records(proof,verified_at=datetime.now(timezone.utc))
        receipt['record_verification']=records
        if records.get('verified') is True:
            private_json(OUTPUT/'publisher-ownership.json',records['ownership'])
            private_json(OUTPUT/'publisher-lease.json',records['lease'])
            receipt['status']='written_and_verified_short_lease'
        else:
            receipt['status']='write_or_readback_unverified_manual_review_required'
        private_json(OUTPUT/'controlled-test-receipt.json',receipt)
        return receipt
    finally:os.close(fd)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply-once',action='store_true',
        help='Requires both private live policies armed and a separately running healthy watchdog')
    parser.add_argument('--refresh',action='store_true',help='Refresh read-only preparation first')
    args=parser.parse_args(argv)
    os.umask(0o077)
    try:
        if args.refresh:
            if args.apply_once:raise ValueError('refresh_and_apply_are_separate_review_steps')
            command=[sys.executable,str(ROOT/'prepare.py'),'--live']
            if PUBLISHER_POLICY.exists():command.extend(['--design-approvals',str(PUBLISHER_POLICY)])
            result=subprocess.run(command,
                capture_output=True,text=True,timeout=300,
                env=task_env())
            if result.returncode:raise ValueError('read_only_preparation_failed')
        result=apply_test() if args.apply_once else prepare_test()
        if args.apply_once:
            print(json.dumps({'status':result['status'],'control_requests':result.get('control_requests')},indent=2))
        else:
            print(json.dumps({'plan':str(OUTPUT/'controlled-test-plan.json'),
                'control_requests':0,'quoted_intervals':result['quoted_interval_count'],
                'live_execution_enabled':False,'plan_blockers':result['plan'].get('reason_codes',[])},indent=2))
        return (0 if not args.apply_once or result.get('status')=='written_and_verified_short_lease' else 2)
    except (ValueError,OSError,KeyError,TypeError,subprocess.SubprocessError):
        print('Controlled test failed; private errors suppressed; control outcome may be unknown.',file=sys.stderr)
        return 1


if __name__=='__main__':raise SystemExit(main())
