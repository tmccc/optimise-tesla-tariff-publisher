"""One-shot orchestration tests: local private fixtures and fake dispatch only."""
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

from date_compiler import build_date_candidate
import publish_once
from tariff_schema import semantic_hash

UTC=timezone.utc
NOW=datetime(2026,11,1,13,0,tzinfo=UTC)
KEEP={'backup_reserve':10,'grid_charging':'not_reported','export_permission':'battery_ok'}


class FixedDateTime(datetime):
    @classmethod
    def now(cls,tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


def fixture_data():
    period={'fromDayOfWeek':0,'toDayOfWeek':6,'fromHour':0,'fromMinute':0,'toHour':0,'toMinute':0}
    def channel(value):
        return {'currency':'GBP','energy_charges':{'Year':{'Flat':value}},
            'seasons':{'Year':{'fromMonth':1,'fromDay':1,'toMonth':12,'toDay':31,
                              'tou_periods':{'Flat':[deepcopy(period)]}}}}
    baseline=channel(.25);baseline['sell_tariff']=channel(.15)
    rows=[]
    for index in range(6):
        start=NOW+index*timedelta(minutes=30)
        rows.append({'valid_from':start.isoformat(),'valid_to':(start+timedelta(minutes=30)).isoformat(),
            'import_gbp_per_kwh':str(Decimal('.30')+Decimal(index)/100),'export_gbp_per_kwh':'.1'})
    preview={'valid':True,'errors':[],'currency':'GBP','timezone':'Europe/London',
        'source_last_success':(NOW-timedelta(seconds=5)).isoformat(),
        'source_captured_at':(NOW-timedelta(seconds=2)).isoformat(),'periods':rows}
    candidate=build_date_candidate(preview,baseline,now=NOW,unquoted_policy='frozen_current_quote')
    assert candidate['candidate_valid'],candidate['errors']
    return baseline,candidate['tariff'],candidate['manifest']


class OneShotTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.output=self.root/'output';self.output.mkdir(mode=0o700)
        self.policy_path=self.root/'publisher-policy.json'
        self.baseline,self.candidate,self.manifest=fixture_data()
        self.old_hash=semantic_hash(self.baseline);self.new_hash=semantic_hash(self.candidate)
        self.approval=str(uuid.uuid4())
        self.policy={'live_execution_enabled':False,'allow_tariff_writes':False,
            'one_shot_only':True,'unquoted_policy':'frozen_current_quote',
            'unquoted_planning_assumption_approved':True,'fallback_design_approved':True,
            'fallback_policy':'self_powered','approved_candidate_semantic_hash':self.new_hash,
            'expected_installed_semantic_hash':self.old_hash,'energy_site_id':123,
            'approval_id':self.approval}
        self.write(self.policy_path,self.policy)
        self.health={'continuous':True,'armed':True,'last_cycle_at':NOW.isoformat(),
                     'pid':2002,'pid_alive':True}
        self.state={'source_last_success':self.manifest['source_last_success'],
            'source_captured_at':self.manifest['source_captured_at'],
            'known_valid_from':self.manifest['known_valid_from'],
            'known_valid_until':self.manifest['known_valid_until'],
            'live_last_success':NOW.isoformat(),'battery_enrolled':False,
            'enrolled_last_success':(NOW-timedelta(seconds=2)).isoformat(),
            'energy_commands_granted':True,'mode':'autonomous','energy_site_id':123,
            'current_tariff_hash':self.old_hash,'expected_baseline_hash':self.old_hash,
            'candidate_tariff_hash':self.new_hash,'candidate_valid':True,
            'candidate_prices_verified':True,'fallback_approved':True,
            'frozen_assumption_approved':True,'live_publication_enabled':True,
            'publisher_pid':2001,'publisher_owns_installed_tariff':False,'ownership':{},
            'watchdog':deepcopy(self.health),'preserve_settings_before':dict(KEEP),
            'preserve_settings':dict(KEEP)}
        self.calls=[];self.journals=[]
        patcher=patch.object(publish_once,'read_active_mode',return_value={
            'mode':'autonomous','mode_source':'fake_local_gateway'})
        patcher.start();self.addCleanup(patcher.stop)
        for target,value in (('ROOT',self.root),('STATE_DIR',self.root),('OUTPUT',self.output),
                             ('PUBLISHER_POLICY',self.policy_path),('datetime',FixedDateTime)):
            patcher=patch.object(publish_once,target,value);patcher.start();self.addCleanup(patcher.stop)

    @staticmethod
    def write(path,value):
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w') as handle:json.dump(value,handle)

    def arm(self):
        self.policy.update(live_execution_enabled=True,allow_tariff_writes=True)
        self.write(self.policy_path,self.policy)

    def context(self,**_kw):
        return deepcopy(self.state),deepcopy(self.candidate),deepcopy(self.manifest)

    def fake_factory(self,*,mode='success',before_callback=None):
        test=self
        class FakeWriter:
            def __init__(self,*,final_authorizer):
                test.calls.append('constructed');self.authorize=final_authorizer

            def publish(self,candidate,*,manifest,expected_site_id,expected_installed_hash,preserve_settings):
                test.calls.append('publish')
                test.assertEqual(semantic_hash(candidate),test.new_hash)
                test.assertEqual(expected_site_id,123)
                test.assertEqual(expected_installed_hash,test.old_hash)
                test.assertEqual(preserve_settings,KEEP)
                test.assertFalse((test.output/'publisher-ownership.json').exists())
                test.assertFalse((test.output/'publisher-lease.json').exists())
                journal=json.loads((test.output/'controlled-test-receipt.json').read_text())
                test.journals.append(deepcopy(journal))
                test.assertEqual(journal['status'],'dispatch_in_progress')
                test.assertIsNone(journal['control_requests'])
                if before_callback:before_callback()
                observations={key:deepcopy(test.state[key]) for key in (
                    'energy_site_id','current_tariff_hash','mode','preserve_settings',
                    'energy_commands_granted','live_last_success','battery_enrolled','enrolled_last_success')}
                observations['candidate_tariff_hash']=test.new_hash
                if self.authorize(observations) is not True:
                    test.calls.append('final_denied')
                    return {'command_sent':False,'write_acknowledged':False,'reason':'fake_final_denied'}
                test.calls.append('fake_post')
                readback=dict(observations,current_tariff_hash=test.new_hash,
                    live_last_success=NOW.isoformat(),observed_at=NOW.isoformat())
                result={'command_sent':True,'write_acknowledged':True,
                    'write_acknowledged_at':(NOW-timedelta(seconds=1)).isoformat(),
                    'fresh_readback_tariff_hash':test.new_hash,'readback':readback}
                if mode=='missing_ack':result['write_acknowledged']=False
                elif mode=='mismatched_readback':
                    result['fresh_readback_tariff_hash']=test.old_hash
                    result['readback']['current_tariff_hash']=test.old_hash
                elif mode=='unknown':
                    result.update(command_sent=None,write_acknowledged=False,
                        fresh_readback_tariff_hash=None,readback={})
                return result
        return FakeWriter

    def apply(self,**factory_kwargs):
        with patch.object(publish_once,'context',side_effect=self.context),\
            patch.object(publish_once,'live_policy_enabled',return_value=True),\
            patch.object(publish_once,'watchdog_health',side_effect=lambda:deepcopy(self.health)):
            return publish_once.apply_test(writer_factory=self.fake_factory(**factory_kwargs))

    def test_disabled_publisher_policy_refuses_before_context_auth_or_network(self):
        factory=Mock()
        with patch.object(publish_once,'context') as context,\
            patch.object(publish_once,'FallbackAdapter') as adapter,\
            patch.object(publish_once,'live_policy_enabled') as fallback:
            result=publish_once.apply_test(writer_factory=factory)
        self.assertEqual(result,{'status':'live_execution_disabled','control_requests':0})
        context.assert_not_called();adapter.assert_not_called();fallback.assert_not_called();factory.assert_not_called()

    def test_prior_dispatch_or_result_blocks_same_approval_before_context(self):
        self.arm()
        for status in ('dispatch_in_progress','write_or_readback_unverified_manual_review_required',
                       'written_and_verified_short_lease'):
            with self.subTest(status=status):
                previous={'approval_id':self.approval,'status':status,'control_requests':None}
                self.write(self.output/'controlled-test-receipt.json',previous)
                with patch.object(publish_once,'context') as context,\
                    patch.object(publish_once,'live_policy_enabled',return_value=True):
                    result=publish_once.apply_test(writer_factory=Mock())
                self.assertEqual(result['status'],'approval_already_attempted_manual_review_required')
                self.assertEqual(result['control_requests'],0)
                context.assert_not_called()
                self.assertEqual(json.loads((self.output/'controlled-test-receipt.json').read_text()),previous)

    def test_preparation_pins_without_arming_or_claiming_watchdog(self):
        self.write(self.output/'frozen_current_quote-proposal.json',
                   {'tou_settings':{'tariff_content_v2':self.candidate}})
        self.write(self.output/'readiness.json',{'candidates':{'frozen_current_quote':{'manifest':self.manifest}},
                   'candidate_comparison':{'all_exact':True}})
        self.write(self.output/'guard-input.json',self.state)
        adapter=Mock();adapter.read_snapshot.return_value=deepcopy(self.state)
        with patch.object(publish_once,'FallbackAdapter',return_value=adapter),\
            patch.object(publish_once,'live_policy_enabled',return_value=False),\
            patch.object(publish_once,'watchdog_health',return_value=deepcopy(self.health)):
            result=publish_once.prepare_test()
        after=json.loads(self.policy_path.read_text())
        self.assertIs(after['live_execution_enabled'],False)
        self.assertIs(after['allow_tariff_writes'],False)
        self.assertEqual(after['expected_installed_semantic_hash'],self.old_hash)
        self.assertEqual(after['approved_candidate_semantic_hash'],self.new_hash)
        self.assertNotEqual(after['approval_id'],self.approval)
        self.assertEqual(str(uuid.UUID(after['approval_id'])),after['approval_id'])
        self.assertFalse(result['live_execution_enabled'])
        self.assertTrue(result['supervised_test'])
        self.assertTrue(result['automatic_fallback_requires_committed_verified_ownership_and_lease'])
        self.assertFalse(result['plan']['allow_write'])
        self.assertFalse((self.output/'publisher-ownership.json').exists())
        self.assertFalse((self.output/'publisher-lease.json').exists())

    def test_preparation_refuses_an_armed_policy_before_snapshot(self):
        self.arm()
        with patch.object(publish_once,'FallbackAdapter') as adapter:
            with self.assertRaises(ValueError):publish_once.prepare_test()
        adapter.assert_not_called()
        self.assertTrue(json.loads(self.policy_path.read_text())['live_execution_enabled'])

    def test_ack_and_fresh_matching_readback_commit_only_after_proof(self):
        self.arm();result=self.apply()
        self.assertEqual(result['status'],'written_and_verified_short_lease')
        self.assertEqual(result['control_requests'],1)
        self.assertEqual(self.calls,['constructed','publish','fake_post'])
        ownership=json.loads((self.output/'publisher-ownership.json').read_text())
        lease=json.loads((self.output/'publisher-lease.json').read_text())
        self.assertTrue(ownership['verified_publisher_write'])
        self.assertEqual(ownership['tariff_semantic_hash'],self.new_hash)
        self.assertTrue(lease['guard_lease_trusted'])
        self.assertEqual(datetime.fromisoformat(lease['guard_lease_until']),NOW+timedelta(minutes=3))
        self.assertEqual(lease['preserve_settings'],KEEP)

    def test_missing_ack_requires_manual_recovery_without_forging_ownership(self):
        self.arm();result=self.apply(mode='missing_ack')
        self.assertEqual(result['status'],'write_or_readback_unverified_manual_review_required')
        self.assertEqual(result['control_requests'],1)
        self.assertFalse(result['record_verification']['verified'])
        self.assertFalse((self.output/'publisher-ownership.json').exists())
        self.assertFalse((self.output/'publisher-lease.json').exists())

    def test_readback_mismatch_never_commits_lease(self):
        self.arm();result=self.apply(mode='mismatched_readback')
        self.assertEqual(result['status'],'write_or_readback_unverified_manual_review_required')
        self.assertFalse((self.output/'publisher-ownership.json').exists())
        self.assertFalse((self.output/'publisher-lease.json').exists())

    def test_unknown_dispatch_counter_stays_unknown_and_cannot_retry(self):
        self.arm();result=self.apply(mode='unknown')
        self.assertIsNone(result['control_requests'])
        calls=list(self.calls)
        again=self.apply()
        self.assertEqual(again['status'],'approval_already_attempted_manual_review_required')
        self.assertEqual(self.calls,calls)
        self.assertFalse((self.output/'publisher-lease.json').exists())

    def test_changed_final_watchdog_rejects_fake_dispatch(self):
        self.arm()
        result=self.apply(before_callback=lambda:self.health.update(pid_alive=False))
        self.assertEqual(result['control_requests'],0)
        self.assertIn('final_denied',self.calls)
        self.assertNotIn('fake_post',self.calls)
        self.assertFalse((self.output/'publisher-lease.json').exists())

    def test_changed_final_approval_id_rejects_fake_dispatch(self):
        self.arm()
        def replace_approval():
            self.policy['approval_id']=str(uuid.uuid4());self.write(self.policy_path,self.policy)
        result=self.apply(before_callback=replace_approval)
        self.assertEqual(result['control_requests'],0)
        self.assertIn('final_denied',self.calls)
        self.assertNotIn('fake_post',self.calls)

    def test_installed_baseline_drift_blocks_before_constructing_sender(self):
        self.arm();self.state['current_tariff_hash']='f'*64
        result=self.apply()
        self.assertEqual(result['status'],'preconditions_blocked')
        self.assertIn('installed_tariff_changed',result['plan']['reason_codes'])
        self.assertEqual(self.calls,[])

    def test_apply_cli_non_success_status_is_nonzero(self):
        with patch.object(publish_once,'apply_test',return_value={
            'status':'preconditions_blocked','control_requests':0}),redirect_stdout(StringIO()):
            self.assertEqual(publish_once.main(['--apply-once']),2)


if __name__=='__main__':unittest.main()
