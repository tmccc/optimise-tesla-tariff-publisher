"""Manual watchdog runner. Defaults to one observation; no automatic startup."""
from __future__ import annotations
import argparse
import fcntl
from datetime import datetime,timedelta,timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time

from prepare import private_json
from runtime import ROOT,FallbackAdapter,live_policy_enabled,private_document
from watchdog import evaluate_cycle
from config import STATE_DIR, ensure_state_dir
from recurring_journal import (optional_private, durable_private_json,
    record_mode_attempt, complete_mode_attempt, reconcile_mode)
from recurring_state import _fresh, _instant

OUTPUT=STATE_DIR/'output'


class CycleDeadline(BaseException):
    """Release the shared lock even when an inner transport catches Exception."""


def _deadline(_signum,_frame):
    raise CycleDeadline()


def _health(*,enabled,continuous,status):
    private_json(OUTPUT/'watchdog-health.json',{
        'pid':os.getpid(),'cycle_completed_at':datetime.now(timezone.utc).isoformat(),
        'continuous':continuous is True,'live_execution_enabled':enabled,
        'state':'armed' if enabled else 'observing','last_status':status,
        'publisher_lease_renewed':False})


def _idle(status,*,enabled,continuous,read_live,reasons=None):
    audit={'status':status,'live_execution_enabled':enabled,
        'command_attempted':False,'command_sent':False,'control_requests':0,
        'reason_codes':reasons or [],'guard_reason_codes':[],
        'publisher_ownership_verified':False,'offline_snapshot_used':not read_live}
    private_json(OUTPUT/'watchdog-observation.json',audit)
    _health(enabled=enabled,continuous=continuous,status=status)
    return audit


class _LockedExecutor:
    def __init__(self,adapter):
        self.adapter=adapter
        self.result=None
        self.snapshot=None

    def read_snapshot(self):
        self.snapshot=self.adapter.read_snapshot()
        return self.snapshot

    def set_self_powered(self,**kwargs):
        self.result=self.adapter.set_self_powered(**kwargs,lock_held=True)
        return self.result


def _locked_observation(*,read_live,continuous,enabled):
    state=optional_private(OUTPUT/'guard-input.json')
    policy=optional_private(STATE_DIR/'execution-policy.json')
    recurring=optional_private(STATE_DIR/'recurring-policy.json')
    owner_path=OUTPUT/'publisher-ownership.json'
    lease_path=OUTPUT/'publisher-lease.json'
    ownership=optional_private(owner_path)
    lease=optional_private(lease_path)
    journal=optional_private(OUTPUT/'recurring-journal.json')
    adapter=FallbackAdapter(ownership,policy_path=STATE_DIR/'execution-policy.json',
        lease_path=lease_path,lock_path=STATE_DIR/'.control.lock',
        mode_attempt_recorder=(lambda context:record_mode_attempt(context,
            controller='watchdog',policy=optional_private(STATE_DIR/'recurring-policy.json')))
            if recurring else None)
    if journal.get('pause_latch'):
        return _idle('persistent_pause',enabled=enabled,continuous=continuous,
            read_live=read_live,reasons=['persistent_manual_or_eon_pause'])
    pending=optional_private(OUTPUT/'mode-intent.json')
    if read_live and enabled:
        enrollment=adapter.enrollment_reader()
        if enrollment.get('battery_enrolled') is True:
            journal['pause_latch']={'kind':'eon','reason':'optimise_battery_enrolled',
                'created_at':datetime.now(timezone.utc).isoformat()}
            durable_private_json(OUTPUT/'recurring-journal.json',journal)
            return _idle('handover_to_optimise',enabled=enabled,continuous=continuous,
                read_live=read_live,reasons=['optimise_battery_enrolled'])
        if (enrollment.get('battery_enrolled') is not False or not _fresh(
            enrollment.get('enrolled_last_success'),timedelta(minutes=45),datetime.now(timezone.utc))):
            return _idle('enrollment_unverified',enabled=enabled,continuous=continuous,
                read_live=read_live,reasons=['optimise_enrollment_not_fresh_false'])
    if read_live and pending:
        observed=adapter.read_snapshot()
        result=reconcile_mode(observed,policy=recurring,now=datetime.now(timezone.utc))
        return _idle('mode_observation_verified' if result.get('verified') else 'mode_outcome_pending',
            enabled=enabled,continuous=continuous,read_live=read_live,
            reasons=result.get('reason_codes'))
    if not lease or not ownership:
        return _idle('waiting_for_publisher_state',enabled=enabled,continuous=continuous,read_live=read_live)
    if lease:
        # Only known guard-input fields may be refreshed from a trusted private
        # publisher lease. This runner never renews it or claims ownership.
        for field in ('source_last_success','source_captured_at','known_valid_from',
            'known_valid_until','guard_lease_until','guard_lease_trusted',
            'last_verified_tariff_hash','intended_tariff_hash'):
            state[field]=lease.get(field)
    # A healthy unexpired lease needs only HA enrollment polling, avoiding
    # several Tesla/Gateway reads per minute while the publisher is healthy.
    until=_instant(lease.get('guard_lease_until'))
    if (read_live and not pending and lease.get('guard_lease_trusted') is True
        and until is not None and until>datetime.now(timezone.utc)):
        return _idle('publisher_lease_current',enabled=enabled,continuous=continuous,read_live=read_live)
    if read_live:state.update(adapter.read_snapshot())
    if read_live and (state.get('mode')=='backup' or
        (journal.get('last_controlled_mode') in {'autonomous','self_consumption'}
            and state.get('mode')!=journal['last_controlled_mode']) or
        (state.get('current_tariff_hash') is not None and
            state.get('current_tariff_hash')!=ownership.get('tariff_semantic_hash'))):
        journal['pause_latch']={'kind':'manual','reason':'manual_mode_change',
            'created_at':datetime.now(timezone.utc).isoformat()}
        durable_private_json(OUTPUT/'recurring-journal.json',journal)
        return _idle('manual_change_pause',enabled=enabled,continuous=continuous,
            read_live=read_live,reasons=['manual_mode_or_tariff_change'])
    state['fallback_approved']=policy.get('fallback_design_approved') is True
    state['fallback_policy']='self_powered'
    # A real publisher must validate a separate watchdog's health; merely
    # starting this runner does not issue a safety attestation or renew a lease.
    state['watchdog_verified']=False
    lease_digest=hashlib.sha256(json.dumps(lease,sort_keys=True).encode()).hexdigest() if lease else None
    previous_path=OUTPUT/'watchdog-last-attempt.json'
    previous=private_document(previous_path) if previous_path.exists() else {}
    already_attempted=bool(lease_digest and previous.get('lease_sha256')==lease_digest)
    executor=_LockedExecutor(adapter)
    audit=evaluate_cycle(state,now=datetime.now(timezone.utc),executor=executor,
        live_execution_enabled=bool(enabled and not already_attempted
            and (not recurring or recurring.get('fallback_approved') is True)))
    if audit.get('command_attempted') and optional_private(OUTPUT/'mode-intent.json'):
        complete_mode_attempt(executor.result or {'command_sent':audit.get('command_sent')})
        if executor.snapshot:
            reconcile_mode(executor.snapshot,policy=recurring,now=datetime.now(timezone.utc))
    audit['duplicate_attempt_blocked']=already_attempted
    audit['actual_mode_observed']=state.get('mode')
    audit['actual_preserve_settings']=state.get('preserve_settings')
    audit['publisher_ownership_verified']=state.get('publisher_owns_installed_tariff') is True
    audit['manual_runner']=True
    audit['offline_snapshot_used']=not read_live
    if audit['command_attempted'] and lease_digest:
        private_json(previous_path,{'lease_sha256':lease_digest,
            'attempted_at':datetime.now(timezone.utc).isoformat(),
            'status':audit['status'],'manual_review_required':True})
    private_json(OUTPUT/'watchdog-observation.json',audit)
    _health(enabled=enabled,continuous=continuous,status=audit['status'])
    return audit


def observe_once(*,read_live=True,continuous=False):
    OUTPUT.mkdir(mode=0o700,parents=True,exist_ok=True)
    enabled=bool(read_live and live_policy_enabled(STATE_DIR/'execution-policy.json'))
    fd=os.open(STATE_DIR/'.control.lock',os.O_CREAT|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return _idle('controller_busy',enabled=enabled,continuous=continuous,read_live=read_live)
        return _locked_observation(read_live=read_live,continuous=continuous,enabled=enabled)
    finally:os.close(fd)


def _run(args):
    while True:
        previous=signal.signal(signal.SIGALRM,_deadline)
        signal.alarm(150)
        try:
            audit=observe_once(read_live=not args.offline,continuous=args.watch)
            print(json.dumps({key:audit.get(key) for key in ('status','live_execution_enabled',
                'actual_mode_observed','publisher_ownership_verified','command_sent',
                'control_requests','reason_codes','guard_reason_codes')},indent=2),flush=True)
        except (CycleDeadline,ValueError,OSError,KeyError,TypeError,subprocess.SubprocessError):
            print(json.dumps({'status':'observation_failed','control_requests':None,
                'private_error_suppressed':True}),flush=True)
            if not args.watch:return 1
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM,previous)
        if not args.watch:return 0
        try:time.sleep(60)
        except KeyboardInterrupt:return 0


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--watch',action='store_true',
        help='Run in this terminal, every 60 seconds; arming remains controlled by disabled policy file')
    parser.add_argument('--offline',action='store_true',
        help='Assess only the saved snapshot, without network requests')
    args=parser.parse_args(argv)
    os.umask(0o077)
    ensure_state_dir()
    fd=os.open(STATE_DIR/'.watchdog.lock',os.O_CREAT|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return _run(args)
    except BlockingIOError:
        print('Another watchdog runner already holds the lock.')
        return 1
    finally:os.close(fd)


if __name__=='__main__':raise SystemExit(main())
