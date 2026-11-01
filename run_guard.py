"""Manual watchdog runner. Defaults to one observation; no automatic startup."""
from __future__ import annotations
import argparse
import fcntl
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from prepare import private_json
from runtime import ROOT,FallbackAdapter,live_policy_enabled,private_document
from watchdog import evaluate_cycle
from config import STATE_DIR, ensure_state_dir

OUTPUT=STATE_DIR/'output'


def observe_once(*,read_live=True,continuous=False):
    state=private_document(OUTPUT/'guard-input.json')
    policy=private_document(STATE_DIR/'execution-policy.json')
    owner_path=OUTPUT/'publisher-ownership.json'
    lease_path=OUTPUT/'publisher-lease.json'
    ownership=private_document(owner_path) if owner_path.exists() else {}
    lease=private_document(lease_path) if lease_path.exists() else None
    adapter=FallbackAdapter(ownership)
    if lease is not None:
        # Only known guard-input fields may be refreshed from a trusted private
        # publisher lease. This runner never renews it or claims ownership.
        for field in ('source_last_success','source_captured_at','known_valid_from',
            'known_valid_until','guard_lease_until','guard_lease_trusted',
            'last_verified_tariff_hash','intended_tariff_hash'):
            state[field]=lease.get(field)
    else:
        state['guard_lease_trusted']=False
        state['publisher_owns_installed_tariff']=False
    if read_live:state.update(adapter.read_snapshot())
    state['fallback_approved']=policy.get('fallback_design_approved') is True
    # A real publisher must validate a separate watchdog's health; merely
    # starting this runner does not issue a safety attestation or renew a lease.
    state['watchdog_verified']=False
    enabled=bool(read_live and live_policy_enabled())
    lease_digest=hashlib.sha256(json.dumps(lease,sort_keys=True).encode()).hexdigest() if lease else None
    previous_path=OUTPUT/'watchdog-last-attempt.json'
    previous=private_document(previous_path) if previous_path.exists() else {}
    already_attempted=bool(lease_digest and previous.get('lease_sha256')==lease_digest)
    if already_attempted:enabled=False
    audit=evaluate_cycle(state,now=datetime.now(timezone.utc),executor=adapter,
                         live_execution_enabled=enabled)
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
    private_json(OUTPUT/'watchdog-health.json',{
        'pid':os.getpid(),'cycle_completed_at':datetime.now(timezone.utc).isoformat(),
        'continuous':continuous is True,'live_execution_enabled':enabled,
        'state':'observing' if enabled is False else 'armed',
        'publisher_lease_renewed':False,
    })
    return audit


def _run(args):
    while True:
        try:
            audit=observe_once(read_live=not args.offline,continuous=args.watch)
            print(json.dumps({key:audit.get(key) for key in ('status','live_execution_enabled',
                'actual_mode_observed','publisher_ownership_verified','command_sent',
                'control_requests','reason_codes','guard_reason_codes')},indent=2),flush=True)
        except (ValueError,OSError,KeyError,TypeError,subprocess.SubprocessError):
            print(json.dumps({'status':'observation_failed','control_requests':None,
                'private_error_suppressed':True}),flush=True)
            if not args.watch:return 1
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
