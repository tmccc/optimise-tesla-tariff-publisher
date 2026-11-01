"""Explicitly armed recurring tariff controller; private state lives outside Git.

One shared control lock serialises the publisher and independent watchdog.
No automatic retry of an uncertain tariff or operating-mode command is allowed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import json
import os
import signal
import time

from config import STATE_DIR, ensure_state_dir
from prepare import private_json
from runtime import FallbackAdapter, live_policy_enabled, private_document
from publish_once import watchdog_health
from ha_reader import read_live_source, read_enrollment
from readback import _get
from active_mode import read_active_mode
from optimise_tariff_preview.compiler import compile_preview
from date_compiler import build_date_candidate
from tariff_schema import semantic_hash, rate_at
from writer import TariffWriter, validate_candidate
from recurring_state import decide_cycle, _instant
from recurring_records import commit_lease
from recurring_journal import optional_private, record_mode_attempt, complete_mode_attempt, reconcile_mode

OUTPUT = STATE_DIR / 'output'
POLICY = STATE_DIR / 'recurring-policy.json'
JOURNAL = OUTPUT / 'recurring-journal.json'
PENDING = OUTPUT / 'tariff-pending.json'


class CycleDeadline(BaseException):
    """Bypass inner transport catches; their finally blocks still close children."""


def _deadline(_signum, _frame):
    raise CycleDeadline()


def now():
    return datetime.now(timezone.utc)


def adapter():
    return FallbackAdapter(optional_private(OUTPUT/'publisher-ownership.json'))


def collect():
    """Fresh explicit HA, Tesla and gateway observations; no control requests."""
    snapshot = adapter().read_snapshot()
    policy = private_document(POLICY)
    state = dict(snapshot, policy=policy, journal=optional_private(JOURNAL),
        ownership=optional_private(OUTPUT/'publisher-ownership.json'),
        lease=optional_private(OUTPUT/'publisher-lease.json'),
        watchdog=watchdog_health(), publisher_pid=os.getpid())
    pending = optional_private(PENDING)
    if pending:
        state['pending'] = pending
    candidate, manifest = None, {}
    try:
        source = read_live_source()
        preview = compile_preview(source, now=now())
        if source.get('source_validation_errors'):
            preview['valid'] = False
        site = snapshot['energy_site_id']
        baseline = _get(f'api/1/energy_sites/{site}/tariff_rate').get('response')
        if semantic_hash(baseline) != snapshot['current_tariff_hash']:
            raise ValueError('tariff_changed_during_collection')
        built = build_date_candidate(preview, baseline, now=now(), unquoted_policy='frozen_current_quote')
        candidate, manifest = built.get('tariff'), built.get('manifest', {})
        exact = bool(candidate and manifest.get('quoted_intervals')) and all(
            rate_at(candidate, _instant(row['valid_from']), direction)
            == Decimal(row[direction+'_gbp_per_kwh'])
            for row in manifest.get('quoted_intervals', []) for direction in ('import','export'))
        state.update({key: manifest.get(key) for key in
            ('source_last_success','source_captured_at','known_valid_from','known_valid_until')})
        state.update(candidate_valid=bool(candidate and built.get('candidate_valid')
            and not validate_candidate(candidate, manifest, now=now())),
            candidate_prices_verified=exact,
            candidate_tariff_hash=semantic_hash(candidate) if candidate else None)
    except Exception:
        state.update(source_read_failed=True, candidate_valid=False, candidate_prices_verified=False)
    return state, candidate, manifest


def _commit(state, *, pending=None):
    records = commit_lease(state, now=now(), pending=pending,
        publication_kind='publication' if pending else 'renewal')
    if records.get('verified') is not True:
        return False
    # Ownership is committed first; a crash before the lease cannot extend safety.
    private_json(OUTPUT/'publisher-ownership.json', records['ownership'])
    private_json(OUTPUT/'publisher-lease.json', records['lease'])
    if pending:
        private_json(OUTPUT/'last-tariff-publication.json', dict(pending,
            resolved_at=now().isoformat(), verified=True, recovery_only=records.get('recovery_only')))
        private_json(PENDING, {})
        state.pop('pending', None)
    state.update(ownership=records['ownership'], lease=records['lease'])
    return True


def _observe_pending(state, read_snapshot):
    pending = state.get('pending')
    if not pending:
        return state
    if pending.get('command_sent') is False:
        private_json(PENDING, {})
        state.pop('pending', None)
        return state
    for _ in range(2):
        snapshot = read_snapshot()
        state.update(snapshot)
        if snapshot.get('current_tariff_hash') != pending.get('candidate_hash'):
            break
        row = {'observed_at': snapshot.get('observed_at'),
            'tariff_hash': snapshot.get('current_tariff_hash'),
            'energy_site_id': snapshot.get('energy_site_id'),
            'preserve_settings': snapshot.get('preserve_settings')}
        pending['matching_readbacks'] = (pending.get('matching_readbacks', [])+[row])[-2:]
        private_json(PENDING, pending)
    state['pending'] = pending
    return state


def _invalidate_lease(state):
    lease = state.get('lease') or {}
    if (lease.get('guard_lease_trusted') is True
        and lease.get('tariff_semantic_hash') == state.get('current_tariff_hash')
        and lease.get('energy_site_id') == state.get('energy_site_id')):
        lease = dict(lease, guard_lease_until=now().isoformat(), invalidated_by='publisher')
        private_json(OUTPUT/'publisher-lease.json', lease)


def _publish(state, candidate, manifest, writer_factory):
    policy_path = STATE_DIR/'publisher-policy.json'
    policy = private_document(policy_path)
    policy.update(approved_candidate_semantic_hash=state['candidate_tariff_hash'],
        expected_installed_semantic_hash=state['current_tariff_hash'])
    private_json(policy_path, policy)
    pending = {'authorized': True, 'kind': 'tariff', 'energy_site_id': state['energy_site_id'],
        'baseline_hash': state['current_tariff_hash'], 'candidate_hash': state['candidate_tariff_hash'],
        'created_at': now().isoformat(), 'policy_generation': state['policy'].get('generation'),
        'command_sent': None, 'dispatch_started': True, 'preserve_settings': state['preserve_settings']}
    private_json(PENDING, pending)

    def authorize(observations):
        fresh = dict(state, **observations)
        active = read_active_mode()
        if (active.get('mode') != state.get('mode')
            or active.get('hardware_preserve_settings') != state.get(
                'active_mode_observation', {}).get('hardware_preserve_settings')):
            return False
        fresh.update(mode=active['mode'], mode_source=active['mode_source'],
            watchdog=watchdog_health(), policy=private_document(POLICY), journal=optional_private(JOURNAL))
        return (live_policy_enabled() and not optional_private(OUTPUT/'mode-intent.json')
            and decide_cycle(fresh, now=now())['action'] == 'refresh_tariff')

    result = writer_factory(final_authorizer=authorize).publish(candidate, manifest=manifest,
        expected_site_id=state['energy_site_id'], expected_installed_hash=state['current_tariff_hash'],
        preserve_settings=state['preserve_settings'], operation_mode=state['mode'])
    pending.update(command_sent=result.get('command_sent'), http_status=result.get('write_http_status'),
        http_completed_at=result.get('write_http_completed_at'), write_accepted=result.get('write_accepted'),
        explicit_rejection=result.get('explicit_rejection') is True,
        reply_metadata=result.get('reply_metadata', {}))
    private_json(PENDING, pending)
    state['pending'] = pending
    return result


def process(state, candidate, manifest, *, writer_factory=TariffWriter, adapter_factory=adapter):
    """Caller owns .control.lock; injected transports permit offline safety tests."""
    mode = reconcile_mode(state, policy=state['policy'], now=now())
    state['journal'] = optional_private(JOURNAL)
    if state.get('battery_enrolled') is True:
        decision = decide_cycle(state, now=now())
        if decision.get('pause_latch'):
            private_json(JOURNAL, dict(state['journal'], pause_latch=decision['pause_latch']))
        return decision
    if mode.get('pending') is True:
        return {'action': 'pause', 'reason_codes': ['mode_command_outcome_pending'],
            'mode_result_pending': True, 'control_requests': 0}
    if state.get('pending'):
        state = _observe_pending(state, adapter_factory().read_snapshot)
    decision = decide_cycle(state, now=now())
    latch = decision.get('pause_latch')
    if latch:
        private_json(JOURNAL, dict(state.get('journal', {}), pause_latch=latch))
    action = decision['action']
    if action == 'safe_fallback':
        _invalidate_lease(state)
    elif action == 'refresh_tariff':
        result = _publish(state, candidate, manifest, writer_factory)
        decision['control_requests'] = 1 if result.get('command_sent') is True else (
            0 if result.get('command_sent') is False else None)
        state = _observe_pending(state, adapter_factory().read_snapshot)
        resolved = decide_cycle(state, now=now())
        if resolved['action'] == 'reconcile_pending':
            decision['publication_verified'] = _commit(state, pending=state['pending'])
        else:
            decision['publication_verified'] = False
            if resolved.get('pause_latch'):
                private_json(JOURNAL, dict(state['journal'], pause_latch=resolved['pause_latch']))
    elif action == 'reconcile_pending':
        decision['publication_verified'] = _commit(state, pending=state['pending'])
    elif action == 'renew_lease':
        decision['lease_renewed'] = _commit(state)
    elif action == 'enable_savings':
        def authorize(observations):
            fresh = dict(state, **observations)
            fresh.update(policy=private_document(POLICY), journal=optional_private(JOURNAL),
                watchdog=watchdog_health(), lease=optional_private(OUTPUT/'publisher-lease.json'))
            return decide_cycle(fresh, now=now())['action'] == 'enable_savings'
        actor = FallbackAdapter(state['ownership'], mode_authorizer=authorize,
            mode_attempt_recorder=lambda context: record_mode_attempt(context,
                controller='publisher', policy=private_document(POLICY)))
        result = actor.set_savings(expected_tariff_hash=state['current_tariff_hash'],
            preserve_settings=state['preserve_settings'], lock_held=True)
        complete_mode_attempt(result)
        decision['control_requests'] = 1 if result.get('command_sent') is True else (
            0 if result.get('command_sent') is False else None)
        decision['mode_result_pending'] = result.get('command_sent') is not False
    decision['actual_mode'] = state.get('mode')
    decision['quoted_intervals'] = manifest.get('quoted_interval_count')
    return decision


def cycle():
    fd = os.open(STATE_DIR/'.control.lock', os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
        state, candidate, manifest = collect()
        result = process(state, candidate, manifest)
        private_json(OUTPUT/'recurring-status.json', dict(result, observed_at=now().isoformat()))
        # Fixed fields only; neither remote arbitrary messages nor identifiers.
        print(json.dumps({key: result.get(key) for key in ('action','reason_codes','actual_mode',
            'control_requests','publication_verified','lease_renewed','quoted_intervals') }), flush=True)
        return result
    except BlockingIOError:
        return {'action': 'pause', 'reason_codes': ['controller_busy'], 'control_requests': 0}
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--watch', action='store_true')
    args = parser.parse_args(argv)
    os.umask(0o077)
    ensure_state_dir()
    OUTPUT.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(STATE_DIR/'.publisher.lock', os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
        while True:
            started = time.monotonic()
            interval = 120
            try:
                previous_handler = signal.signal(signal.SIGALRM, _deadline)
                signal.alarm(150)
                try:
                    result = cycle()
                finally:
                    signal.alarm(0)
                    signal.signal(signal.SIGALRM, previous_handler)
                if (result.get('publication_verified') is True
                    or (result.get('lease_renewed') is True and result.get('actual_mode') == 'self_consumption')):
                    interval = 5
                elif result.get('mode_result_pending') is True:
                    interval = 15
            except (Exception, CycleDeadline):
                print(json.dumps({'action':'pause','reason_codes':['private_cycle_error_suppressed'],
                    'control_requests':None}), flush=True)
                if not args.watch:
                    return 1
            if not args.watch:
                return 0
            time.sleep(max(1, interval-(time.monotonic()-started)))
    except BlockingIOError:
        print('Another recurring publisher already holds its lock.')
        return 1
    finally:
        os.close(fd)


if __name__ == '__main__':
    raise SystemExit(main())
