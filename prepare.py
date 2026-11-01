"""Prepare and assess tariff proposals. This module has no control endpoint.

Offline replay requires explicit input and baseline files. --live makes only
configured HA/Tessie/gateway reads. This preparation module sends no controls.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import stat

from config import ROOT, STATE_DIR, ensure_state_dir
from optimise_tariff_preview.compiler import compile_preview
from ha_reader import read_live_source, read_battery_status
from readback import read_live_readback
from date_compiler import build_date_candidate
from guard import evaluate_guard
from tariff_schema import canonical_hash, rate_at, semantic_hash, to_upload_tariff, validate_tariff

UTC = timezone.utc
OUTPUT_MARKER = '.optimise-tesla-output.json'
OUTPUT_IDENTITY = {'tool': 'optimise-tesla-tariff-publisher', 'output_schema': 1}


def instant(value):
    value = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('aware_timestamp_required')
    return value.astimezone(UTC)


def _enrollment(status):
    rows = status.get('states', [])
    enrolled = [row for row in rows if row.get('key') == 'battery_enrolled']
    stamps = [row for row in rows if row.get('key') == 'status_last_update']
    diagnostics = status.get('diagnostics', [])
    if len(enrolled) != 1 or len(stamps) != 1 or len(diagnostics) != 1:
        return None, None
    section = diagnostics[0].get('status', {})
    failed = (section.get('provider_error') or {}).get('failed_roots', [])
    if section.get('fresh') is not True or section.get('last_update_success') is not True:
        return None, None
    if 'smartshiftExperience' in failed:
        return None, None
    return {'on': True, 'off': False}.get(enrolled[0].get('state')), stamps[0].get('state')


def prepare_report(source, backup, readback, status, *, now, design_approvals=None):
    """Pure assessment, preserving approval/watchdog prerequisites as false."""
    clock = instant(now)
    approvals = design_approvals if isinstance(design_approvals, dict) else {}
    fallback_approved = (approvals.get('fallback_design_approved') is True
        and approvals.get('fallback_policy') == 'self_powered')
    frozen_approved = (approvals.get('unquoted_planning_assumption_approved') is True
        and approvals.get('unquoted_policy') == 'frozen_current_quote')
    tariff = backup.get('tariff_rate_response')
    structural = validate_tariff(tariff, strict_compatibility=False)
    strict = validate_tariff(tariff)
    digest = canonical_hash(tariff)
    integrity = digest == backup.get('sha256') == readback.get('tariff_sha256')
    report = {
        'schema_version': 1, 'generated_at': clock.isoformat(),
        'preparation_only': True, 'tesla_control_requests': 0,
        'ready_for_upload': False, 'watchdog_installed': False,
        'fallback_approved': fallback_approved, 'live_execution_enabled': False,
        'unquoted_policy_approved': 'frozen_current_quote' if frozen_approved else None,
        'onboarding_or_permissions_changed': False,
        'backup': {'sha256': digest, 'integrity_verified': integrity,
            'structural_errors': structural, 'upload_compatibility_errors': strict,
            'restoration_verified': False,
            'exact_restore_through_documented_api_supported': integrity and not strict},
        'limits': [
            'Tesla seasons repeat annually and contain no server-enforced expiry.',
            'A host watchdog cannot change Tesla mode while this host or its network is unavailable.',
            'Unquoted placeholder values are not prices and must not drive Savings.',
            'The fallback runner is built but remains unarmed and is not running.',
            'Tariff restoration cannot restore supplier-link or onboarding state.',
        ],
    }
    if structural or not integrity:
        report['blockers'] = ['backup_structure_or_integrity_failed']
        return report, {}
    preview = compile_preview(source, now=clock)
    if source.get('source_validation_errors'):
        preview['valid'] = False
        preview['errors'].append({'code': 'source_metadata_validation_failed'})
    preview['ready_for_upload'] = False
    report['prices'] = {'valid': preview['valid'],
        'errors': [row['code'] for row in preview['errors']],
        'last_success': preview.get('source_last_success'),
        'captured_at': preview.get('source_captured_at')}
    candidates = {}
    for policy in ('preserve_baseline', 'disabled_zero_placeholder', 'frozen_current_quote'):
        candidates[policy] = build_date_candidate(preview, tariff, now=clock, unquoted_policy=policy)
    proposed = candidates['frozen_current_quote']
    proposed.setdefault('manifest',{})['approved_design_assumptions']={
        'frozen_current_quote':frozen_approved,'self_powered_failure_policy':fallback_approved,
        'live_execution':False}
    approved_blockers = set()
    if frozen_approved:approved_blockers.add('unquoted_lookahead_assumption_unapproved')
    if fallback_approved:approved_blockers.add('expiry_handoff_unapproved')
    proposed['manifest']['activation_blockers']=[code for code in
        proposed['manifest'].get('activation_blockers',[]) if code not in approved_blockers]
    proposed_tariff = proposed.get('tariff')
    compared = []
    if proposed_tariff is not None:
        for row in proposed['manifest']['quoted_intervals']:
            for direction in ('import', 'export'):
                expected = Decimal(row[direction + '_gbp_per_kwh'])
                compared.append(rate_at(proposed_tariff, instant(row['valid_from']), direction) == expected)
    enrolled, enrolled_stamp = _enrollment(status)
    manifest = proposed.get('manifest', {})
    installed_semantics = semantic_hash(tariff)
    guard_state = {
        'source_last_success': preview.get('source_last_success'),
        'source_captured_at': preview.get('source_captured_at'),
        'known_valid_from': manifest.get('known_valid_from'),
        'known_valid_until': manifest.get('known_valid_until'),
        'live_last_success': readback.get('captured_at'),
        'battery_enrolled': enrolled, 'enrolled_last_success': enrolled_stamp,
        'energy_commands_granted': readback.get('energy_commands_scope'),
        'mode': readback.get('active_mode', readback.get('site_settings', {}).get('default_real_mode')),
        'current_tariff_hash': installed_semantics,
        'last_verified_tariff_hash': installed_semantics,
        'intended_tariff_hash': semantic_hash(proposed_tariff) if proposed_tariff else None,
        'guard_lease_until': None, 'watchdog_verified': False,
        'fallback_policy': 'self_powered', 'fallback_approved': fallback_approved,
        'publisher_owns_installed_tariff': False,
    }
    actual = evaluate_guard(guard_state, now=clock)
    report['current_settings'] = readback.get('site_settings', {})
    report['optimise_battery_enrolled'] = enrolled
    report['candidate_comparison'] = {
        'price_values_compared': len(compared), 'all_exact': bool(compared) and all(compared),
        'different_date_quotes_are_not_remapped': True,
    }
    report['candidates'] = {policy: {
        'candidate_valid': candidate['candidate_valid'],
        'errors': candidate['errors'], 'manifest': candidate['manifest'],
        'ready_for_upload': False,
    } for policy, candidate in candidates.items()}
    report['guard'] = actual
    report['proposed_failure_action'] = {
        'status': 'design_approved_execution_disabled' if fallback_approved and frozen_approved else 'design_requires_approval',
        'while_optimise_confirmed_unenrolled': 'Switch Tesla to Self-Powered on unusable/stale prices or expiry.',
        'when_optimise_enrolled': 'Stop publishing and hand over; do not override E.ON control.',
        'when_optimise_status_unknown': 'Pause publishing and require controller verification.',
        'preserve': ['backup_reserve', 'grid_charging_permission', 'export_permission', 'Amber_authorisation'],
    }
    report['blockers'] = list(actual['reason_codes'])
    report['blockers'] += ['live_test_not_executed', 'live_execution_disabled',
        'native_expiry_unavailable', 'billing_semantics_not_independently_verified']
    if not frozen_approved:report['blockers'].append('unquoted_planning_assumption_unapproved')
    if strict:report['blockers'].append('backup_not_exactly_restorable')
    if not proposed['candidate_valid'] or not report['candidate_comparison']['all_exact']:
        report['blockers'].append('candidate_validation_failed')
    # These are deliberately offline simulations with fictional prerequisites,
    # never claims that a watchdog or approved control path actually exists.
    scenarios = {}
    for name, changes in {
        'expired_quotes': {'known_valid_until': clock.isoformat()},
        'stale_prices': {'source_last_success': (clock - timedelta(minutes=16)).isoformat()},
        'optimise_activates': {'battery_enrolled': True},
        'optimise_status_lost': {'battery_enrolled': None},
        'tariff_changed_elsewhere': {'current_tariff_hash': 'f' * 64},
    }.items():
        sample = dict(guard_state, watchdog_verified=True, fallback_approved=True,
                      publisher_owns_installed_tariff=True,
                      guard_lease_until=(clock + timedelta(minutes=1)).isoformat())
        sample.update(changes)
        decision = evaluate_guard(sample, now=clock)
        scenarios[name] = {'simulation_only': True,
            'allow_publish': decision['allow_publish'],
            'proposed_action': decision['proposed_action'],
            'action_executed': False, 'reason_codes': decision['reason_codes']}
    report['guard_simulations'] = scenarios
    artifacts = {'source.json': source, 'preview.json': preview,
        'baseline-format-only-NOT-VERIFIED-ROLLBACK.json':
            {'tou_settings': {'tariff_content_v2': to_upload_tariff(tariff)}},
        'guard-input.json': guard_state}
    for policy, candidate in candidates.items():
        if candidate.get('tariff') is not None:
            artifacts[policy + '-proposal.json'] = {'tou_settings': {'tariff_content_v2': candidate['tariff']}}
    return report, artifacts


def private_json(path, value):
    temp = path.parent / ('.' + path.name + '.' + str(os.getpid()))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle, indent=2, allow_nan=False)
            handle.write('\n')
        if path.is_symlink():
            raise ValueError('output_symlink_rejected')
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def persist_outputs(output, report, artifacts):
    """Remove only owned stale artifacts after a blocked preparation run."""
    known={'source.json','preview.json','baseline-format-only-NOT-VERIFIED-ROLLBACK.json',
        'guard-input.json','preserve_baseline-proposal.json',
        'disabled_zero_placeholder-proposal.json','frozen_current_quote-proposal.json',
        'controlled-test-plan.json','baseline-backup.json'}
    if not set(artifacts)<=known:raise ValueError('unexpected_artifact')
    if output.is_symlink():raise ValueError('output_symlink_rejected')
    output.mkdir(mode=0o700,exist_ok=True)
    directory=output.stat()
    if not stat.S_ISDIR(directory.st_mode) or directory.st_uid!=os.getuid():
        raise ValueError('output_directory_owner_invalid')
    marker=output/OUTPUT_MARKER
    if marker.is_symlink():raise ValueError('output_marker_symlink_rejected')
    if marker.exists():
        fd=os.open(marker,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            info=os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid()
                or not 1<=info.st_size<=4096):
                raise ValueError('output_marker_invalid')
            with os.fdopen(fd,'rb',closefd=False) as handle:raw=handle.read(4097)
        finally:os.close(fd)
        try:
            if len(raw)>4096 or json.loads(raw)!=OUTPUT_IDENTITY:
                raise ValueError('output_marker_invalid')
        except (ValueError,UnicodeError):
            raise ValueError('output_marker_invalid') from None
    elif any(output.iterdir()):
        raise ValueError('unowned_output_directory')
    # Directory permissions and contents are touched only after ownership has
    # been established, or after claiming a new/empty caller-selected directory.
    output.chmod(0o700)
    if not marker.exists():private_json(marker,OUTPUT_IDENTITY)
    for name in known-set(artifacts):
        path=output/name
        if path.is_symlink():raise ValueError('stale_output_symlink_rejected')
        path.unlink(missing_ok=True)
    for name,value in artifacts.items():private_json(output/name,value)
    private_json(output/'readiness.json',report)


def _input_object(path):
    if path.is_symlink():raise ValueError('input_symlink_rejected')
    with path.open('rb') as handle:raw=handle.read(2*1024*1024+1)
    if len(raw)>2*1024*1024:raise ValueError('input_oversized')
    value=json.loads(raw)
    if not isinstance(value,dict):raise ValueError('input_object_required')
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source_group=parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument('--input',type=Path,help='Offline canonical price snapshot; never makes requests')
    source_group.add_argument('--live',action='store_true',help='Explicit read-only HA/Tessie/gateway requests using configured token files')
    parser.add_argument('--baseline',type=Path,help='Required with --input: plain tariff JSON or private tariff backup')
    parser.add_argument('--as-of',help='Explicit aware clock for offline replay only')
    parser.add_argument('--output-dir',type=Path,default=STATE_DIR/'output')
    parser.add_argument('--design-approvals',type=Path,help='Optional explicit design policy; defaults to all approvals false')
    args=parser.parse_args(argv)
    if args.input is not None and args.baseline is None:parser.error('--input requires --baseline')
    if args.live and (args.baseline is not None or args.as_of is not None):
        parser.error('--baseline/--as-of are restricted to offline input')
    os.umask(0o077)
    ensure_state_dir()
    fd=os.open(STATE_DIR/'.prepare.lock',os.O_CREAT|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        clock=instant(args.as_of) if args.as_of else datetime.now(UTC)
        approvals=_input_object(args.design_approvals) if args.design_approvals else {}
        if args.live:
            source=read_live_source()
            status=read_battery_status()
            backup,readback=read_live_readback()
            clock=datetime.now(UTC)
        else:
            source=_input_object(args.input)
            baseline=_input_object(args.baseline)
            backup=baseline if 'tariff_rate_response' in baseline else {
                'tariff_rate_response':baseline,'sha256':canonical_hash(baseline)}
            readback={'tariff_sha256':backup.get('sha256'),'captured_at':None,
                'energy_commands_scope':None,'site_settings':{},'source':'offline supplied baseline; not a live observation'}
            status={'states':[],'diagnostics':[]}
        report,artifacts=prepare_report(source,backup,readback,status,now=clock,design_approvals=approvals)
        report['source_mode']='live_read_only' if args.live else 'offline_replay'
        report['assessment_clock_explicit']=args.as_of is not None
        if args.live:artifacts['baseline-backup.json']=backup
        persist_outputs(args.output_dir,report,artifacts)
        print(json.dumps({'report':str(args.output_dir/'readiness.json'),
            'ready_for_upload':False,'tesla_control_requests':0,'source_mode':report['source_mode'],
            'backup_compatibility':report['backup']['upload_compatibility_errors'],
            'candidate_comparison':report.get('candidate_comparison'),'blockers':report.get('blockers',[])},indent=2))
        return 0
    except (ValueError,OSError,KeyError,TypeError):
        print('Tariff preparation failed; private diagnostics suppressed.',file=sys.stderr)
        return 1
    finally:os.close(fd)


if __name__=='__main__':raise SystemExit(main())
