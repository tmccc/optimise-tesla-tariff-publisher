"""Pure recurring-controller decisions; no IO, credentials or control requests.

The caller supplies real fresh observations, preserves any returned pause latch,
and journals writes/mode actions durably under the shared control lock. An action
is permission to attempt a separately gated operation, never evidence it ran.
Pending writes are resolved before a later tariff may be dispatched.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import re
from typing import Any

UTC = timezone.utc
SOURCE_MAX_AGE = timedelta(minutes=15)
ENROLLMENT_MAX_AGE = timedelta(minutes=45)
LIVE_MAX_AGE = timedelta(minutes=5)
WATCHDOG_MAX_AGE = timedelta(seconds=90)
MAX_LEASE = timedelta(minutes=5)
MIN_LEASE_HEADROOM = timedelta(seconds=30)
MIN_SAVINGS_LEASE = timedelta(seconds=60)
HASH = re.compile(r'[0-9a-f]{64}\Z')
MODES = frozenset({'autonomous', 'self_consumption', 'backup'})
EXPORT_RULES = frozenset({'battery_ok', 'pv_only', 'never'})
FALLBACK_CONTROLLERS = frozenset({'publisher', 'watchdog'})


def _instant(value: Any) -> datetime | None:
    if isinstance(value, str):
        if len(value) > 80:
            return None
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except (ValueError, OverflowError):
            return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    try:
        if value.utcoffset() is None:
            return None
        return value.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def _hash(value: Any) -> bool:
    return isinstance(value, str) and HASH.fullmatch(value) is not None


def _site(value: Any) -> bool:
    return type(value) is int and 0 < value < 2**63


def _fresh(value: Any, maximum: timedelta, clock: datetime) -> bool:
    stamp = _instant(value)
    return bool(stamp is not None and timedelta(0) <= clock - stamp < maximum)


def _protected(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    reserve, grid, export = (value.get(key) for key in
        ('backup_reserve', 'grid_charging', 'export_permission'))
    return (type(reserve) in {int, float} and 0 <= reserve <= 100
        and math.isfinite(reserve)
        and (type(grid) is bool or grid == 'not_reported')
        and isinstance(export, str) and export in EXPORT_RULES)


def _ownership_record_valid(owner: Any, site: Any, clock: datetime) -> bool:
    if (not isinstance(owner, dict) or owner.get('verified_publisher_write') is not True
        or not _site(site) or owner.get('energy_site_id') != site
        or not _hash(owner.get('tariff_semantic_hash'))):
        return False
    written, verified = _instant(owner.get('written_at')), _instant(owner.get('last_verified_at'))
    return bool(written is not None and verified is not None and written <= verified <= clock)


def _watchdog_ready(state: dict, clock: datetime) -> bool:
    health = state.get('watchdog')
    if not isinstance(health, dict):
        return False
    ours, theirs = state.get('publisher_pid'), health.get('pid')
    return (type(ours) is int and 0 < ours < 2**31
        and type(theirs) is int and 0 < theirs < 2**31 and ours != theirs
        and all(health.get(field) is True for field in ('continuous', 'armed', 'pid_alive'))
        and _fresh(health.get('last_cycle_at'), WATCHDOG_MAX_AGE, clock))


def _source_reasons(state: dict, clock: datetime) -> list[str]:
    reasons = []
    if state.get('source_read_failed') is True:
        reasons.append('price_source_read_failed')
    for field in ('source_last_success', 'source_captured_at'):
        if not _fresh(state.get(field), SOURCE_MAX_AGE, clock):
            reasons.append(field + '_not_fresh')
    success, captured = _instant(state.get('source_last_success')), _instant(state.get('source_captured_at'))
    if success is not None and captured is not None and success > captured:
        reasons.append('source_success_after_capture')
    start, end = _instant(state.get('known_valid_from')), _instant(state.get('known_valid_until'))
    if start is None or end is None or not start <= clock < end:
        reasons.append('quoted_window_not_current')
    if state.get('candidate_valid') is not True:
        reasons.append('candidate_not_valid')
    if state.get('candidate_prices_verified') is not True:
        reasons.append('candidate_prices_not_verified')
    if not _hash(state.get('candidate_tariff_hash')):
        reasons.append('candidate_hash_invalid')
    return reasons


def proposed_lease_until(state: dict, *, now: datetime) -> str | None:
    """Clip a fresh lease; valid even while the device is still Self-Powered.

    This function does not claim ownership, publish prices or renew a heartbeat.
    Callers must separately pass controller/health/ownership prerequisites.
    """
    clock = _instant(now)
    if clock is None:
        raise ValueError('aware_assessment_time_required')
    if not isinstance(state, dict) or _source_reasons(state, clock):
        return None
    if not _fresh(state.get('live_last_success'), LIVE_MAX_AGE, clock):
        return None
    if not _fresh(state.get('enrolled_last_success'), ENROLLMENT_MAX_AGE, clock):
        return None
    limits = [clock + MAX_LEASE, _instant(state['known_valid_until'])]
    for field, maximum in (('source_last_success', SOURCE_MAX_AGE),
        ('source_captured_at', SOURCE_MAX_AGE), ('live_last_success', LIVE_MAX_AGE),
        ('enrolled_last_success', ENROLLMENT_MAX_AGE)):
        limits.append(_instant(state[field]) + maximum)
    deadline = min(limits)
    return deadline.isoformat() if deadline - clock >= MIN_LEASE_HEADROOM else None


def _own_fallback(journal: dict, policy: dict, site: int, clock: datetime) -> bool:
    action = journal.get('last_mode_action')
    if not isinstance(action, dict):
        return False
    verified = _instant(action.get('verified_at'))
    return (action.get('controller') in FALLBACK_CONTROLLERS
        and action.get('target') == 'self_consumption'
        and action.get('verified') is True and action.get('energy_site_id') == site
        and verified is not None and verified <= clock
        and _hash(action.get('tariff_semantic_hash'))
        and (policy.get('generation') is None
            or action.get('policy_generation') == policy.get('generation')))


def _valid_pending(pending: Any, site: int, clock: datetime) -> bool:
    if not isinstance(pending, dict):
        return False
    created = _instant(pending.get('created_at'))
    return (pending.get('authorized') is True and pending.get('energy_site_id') == site
        and _hash(pending.get('baseline_hash')) and _hash(pending.get('candidate_hash'))
        and pending['baseline_hash'] != pending['candidate_hash']
        and created is not None and created <= clock
        and pending.get('kind', 'tariff') == 'tariff'
        and pending.get('explicit_rejection') is not True
        and pending.get('write_accepted') is not False)


def _target_observed_twice(pending: dict, state: dict, clock: datetime) -> bool:
    observations = pending.get('matching_readbacks')
    if not isinstance(observations, list) or len(observations) < 2:
        return False
    first, second = observations[-2:]
    if not isinstance(first, dict) or not isinstance(second, dict):
        return False
    first_at, second_at = _instant(first.get('observed_at')), _instant(second.get('observed_at'))
    return (pending.get('http_status') == 200 and type(pending.get('http_status')) is int
        and pending.get('command_sent') is True and pending.get('write_accepted') is not False
        and first_at is not None and second_at is not None
        and _instant(pending['created_at']) <= first_at < second_at <= clock
        and _fresh(first_at, LIVE_MAX_AGE, clock) and _fresh(second_at, LIVE_MAX_AGE, clock)
        and first.get('tariff_hash') == second.get('tariff_hash') == pending['candidate_hash']
        and first.get('energy_site_id') == second.get('energy_site_id') == state['energy_site_id']
        and _protected(pending.get('preserve_settings'))
        and _protected(first.get('preserve_settings'))
        and pending.get('preserve_settings') == first.get('preserve_settings')
            == second.get('preserve_settings') == state.get('preserve_settings'))


def decide_cycle(state: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    """Decide one recurring cycle without IO or effects.

    Persistent journal fields: pause_latch, bootstrap_consumed,
    last_controlled_mode and last_mode_action. A verified own fallback action
    names controller publisher/watchdog, target self_consumption, site, optional
    policy_generation and verified=True. Clearing a pause latch is an explicit
    operator task; fresh telemetry alone never clears a handover/manual latch.
    """
    clock = _instant(now)
    if clock is None:
        raise ValueError('aware_assessment_time_required')
    if not isinstance(state, dict):
        state = {}
    policy = state.get('policy') if isinstance(state.get('policy'), dict) else {}
    journal = state.get('journal') if isinstance(state.get('journal'), dict) else {}
    result = {'schema_version': 1, 'assessed_at': clock.isoformat(), 'action': 'pause',
        'reason_codes': [], 'pause_latch': None, 'allow_write': False,
        'allow_mode_change': False, 'allow_lease_renewal': False, 'lease_until': None,
        'owns_installed_tariff': False, 'pending_requires_resolution': False,
        'expired_recovery_only': False, 'source_valid': False,
        'control_requests': 0, 'effects_executed': False,
        'preserve_settings': ['backup_reserve', 'grid_charging', 'export_permission']}

    def decide(action, *reasons, latch=None):
        result.update(action=action, reason_codes=list(reasons), pause_latch=latch)
        result['allow_write'] = action == 'refresh_tariff'
        result['allow_mode_change'] = action in {'safe_fallback', 'enable_savings'}
        result['allow_lease_renewal'] = action == 'renew_lease'
        return result

    def latch(kind, reason):
        return {'kind': kind, 'reason': reason, 'created_at': clock.isoformat()}

    existing_latch = journal.get('pause_latch')
    if existing_latch:
        kind = existing_latch.get('kind') if isinstance(existing_latch, dict) else None
        known = kind in {'manual', 'eon'}
        allowed_reasons = {'optimise_battery_enrolled','energy_site_identity_mismatch',
            'special_or_manual_mode','pending_record_invalid_or_rejected',
            'pending_installed_hash_unrelated','external_tariff_change',
            'self_powered_not_owned_or_bootstrapped','installed_tariff_ownership_unverified',
            'manual_mode_change','operator_requested_pause'}
        previous_at = _instant(existing_latch.get('created_at')) if known else None
        normalized = ({'kind': kind,
            'reason': existing_latch.get('reason') if existing_latch.get('reason') in allowed_reasons
                else 'persistent_pause',
            'created_at': previous_at.isoformat() if previous_at is not None and previous_at<=clock
                else clock.isoformat()} if known else latch('manual', 'unknown_existing_latch'))
        return decide('handover' if kind == 'eon' else 'pause',
            'persistent_handover_latch' if kind == 'eon' else 'persistent_manual_pause', latch=normalized)
    if state.get('battery_enrolled') is True:
        return decide('handover', 'optimise_battery_enrolled', latch=latch('eon', 'optimise_battery_enrolled'))
    if policy.get('enabled') is not True:
        return decide('pause', 'recurring_policy_disabled')
    if (state.get('battery_enrolled') is not False
        or not _fresh(state.get('enrolled_last_success'), ENROLLMENT_MAX_AGE, clock)):
        return decide('pause', 'optimise_enrollment_not_freshly_unenrolled')
    if (not _fresh(state.get('live_last_success'), LIVE_MAX_AGE, clock)
        or state.get('mode_source') != 'local_gateway_TEDAPI'):
        return decide('pause', 'active_live_observation_unavailable')
    site = state.get('energy_site_id')
    if not _site(site) or policy.get('energy_site_id') != site:
        return decide('pause', 'energy_site_identity_mismatch', latch=latch('manual', 'energy_site_identity_mismatch'))
    if state.get('mode') not in MODES:
        return decide('pause', 'active_mode_unknown')
    if state['mode'] == 'backup':
        return decide('pause', 'special_or_manual_mode', latch=latch('manual', 'special_or_manual_mode'))
    if state.get('energy_commands_granted') is not True:
        return decide('pause', 'energy_commands_unavailable')
    if not _hash(state.get('current_tariff_hash')) or not _protected(state.get('preserve_settings')):
        return decide('pause', 'tariff_or_protected_observation_unavailable')
    owner = state.get('ownership')
    owner_record = _ownership_record_valid(owner, site, clock)
    owns = owner_record and owner['tariff_semantic_hash'] == state['current_tariff_hash']
    result['owns_installed_tariff'] = bool(owns)
    source_reasons = _source_reasons(state, clock)
    result['source_valid'] = not source_reasons
    health = _watchdog_ready(state, clock)
    fallback = policy.get('fallback_approved') is True
    lease = state.get('lease') if isinstance(state.get('lease'), dict) else {}
    expiry = _instant(lease.get('guard_lease_until'))
    issued = _instant(lease.get('issued_at'))
    lease_matches = (lease.get('guard_lease_trusted') is True
        and lease.get('verified_publisher_write') is True
        and lease.get('publisher_owns_installed_tariff') is True
        and lease.get('energy_site_id') == site
        and lease.get('tariff_semantic_hash') == state['current_tariff_hash']
        and issued is not None and issued <= clock and expiry is not None)
    expired = not lease_matches or expiry <= clock

    pending = state.get('pending')
    if pending is not None:
        result['pending_requires_resolution'] = True
        if not _valid_pending(pending, site, clock):
            return decide('pause', 'pending_record_invalid_or_rejected',
                latch=latch('manual', 'pending_record_invalid_or_rejected'))
        if state['current_tariff_hash'] == pending['candidate_hash']:
            if _target_observed_twice(pending, state, clock):
                result['expired_recovery_only'] = bool(source_reasons)
                return decide('reconcile_pending', 'pending_target_verified_twice')
            return decide('pause', 'pending_target_needs_independent_readbacks')
        if state['current_tariff_hash'] == pending['baseline_hash']:
            if owns and state['mode'] == 'autonomous' and fallback and (expired or source_reasons or not health):
                return decide('safe_fallback', 'pending_unresolved_on_owned_baseline',
                    'invalidate_owned_lease_before_mode_command')
            return decide('pause', 'pending_outcome_unresolved_no_resend')
        return decide('pause', 'pending_installed_hash_unrelated',
            latch=latch('manual', 'pending_installed_hash_unrelated'))

    if owner_record and not owns:
        return decide('pause', 'external_tariff_change', latch=latch('manual', 'external_tariff_change'))
    bootstrap = (policy.get('bootstrap_from_self_powered') is True
        and journal.get('bootstrap_consumed') is not True)
    own_fallback = _own_fallback(journal, policy, site, clock)
    resume = own_fallback and policy.get('resume_after_own_fallback') is True
    if (state['mode']=='self_consumption' and journal.get('last_controlled_mode')=='autonomous'
        and not own_fallback):
        return decide('pause','manual_mode_change',latch=latch('manual','manual_mode_change'))
    if state['mode'] == 'self_consumption' and not (bootstrap or resume):
        return decide('pause', 'self_powered_not_owned_or_bootstrapped',
            latch=latch('manual', 'self_powered_not_owned_or_bootstrapped'))
    if source_reasons or not health:
        reasons = list(source_reasons) + ([] if health else ['independent_watchdog_unavailable'])
        if owns and state['mode'] == 'autonomous' and fallback:
            return decide('safe_fallback', *reasons, 'invalidate_owned_lease_before_mode_command')
        return decide('pause', *reasons)
    if policy.get('frozen_assumption_approved') is not True or not fallback:
        return decide('pause', 'design_approval_missing')
    proposed = proposed_lease_until(state, now=clock)
    if proposed is None:
        if owns and state['mode'] == 'autonomous' and fallback:
            return decide('safe_fallback', 'insufficient_fresh_lease_headroom',
                'invalidate_owned_lease_before_mode_command')
        return decide('pause', 'insufficient_fresh_lease_headroom')
    result['lease_until'] = proposed
    same_candidate = state['current_tariff_hash'] == state['candidate_tariff_hash']
    if not owns:
        approved_baseline = policy.get('bootstrap_baseline_hash')
        if (bootstrap and state['mode'] == 'self_consumption'
            and _hash(approved_baseline) and approved_baseline == state['current_tariff_hash']
            and not same_candidate):
            return decide('refresh_tariff', 'explicit_initial_self_powered_baseline')
        return decide('pause', 'installed_tariff_ownership_unverified',
            latch=latch('manual', 'installed_tariff_ownership_unverified'))
    if not same_candidate:
        return decide('refresh_tariff', 'quoted_tariff_changed')
    if state['mode'] == 'self_consumption':
        if policy.get('manage_savings') is not True:
            return decide('pause', 'savings_mode_management_disabled')
        if (not lease_matches or expiry - clock < MIN_SAVINGS_LEASE
            or expiry > _instant(proposed)):
            return decide('renew_lease', 'verified_lease_required_before_savings')
        return decide('enable_savings', 'fresh_owned_tariff_and_guard_lease')
    return decide('renew_lease', 'same_verified_tariff_no_post')
