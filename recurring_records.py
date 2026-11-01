"""Pure evidence records for the recurring controller; no IO or effects.

Lease renewal cannot claim another tariff POST. Pending publication recovery
needs the coordinator's exact before/after proof, including two independent
readbacks. An uncertain application acknowledgement remains uncertain even
when the resulting installed tariff is independently verified.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any

from recurring_state import (
    ENROLLMENT_MAX_AGE, LIVE_MAX_AGE, MAX_LEASE, _fresh, _hash, _instant,
    _protected, _site, _watchdog_ready, decide_cycle, proposed_lease_until,
)

SOURCE_FIELDS = ('source_last_success', 'source_captured_at',
    'known_valid_from', 'known_valid_until')


def _source_metadata(state: dict) -> dict:
    return {key: value.isoformat() if (value := _instant(state.get(key)))
        is not None else None for key in SOURCE_FIELDS}


def _keep(value: dict) -> dict:
    return {key: value[key] for key in
        ('backup_reserve', 'grid_charging', 'export_permission')}


def commit_lease(state: dict[str, Any], *, now: datetime,
        publication_kind: str = 'renewal', pending: dict | None = None) -> dict:
    """Return ownership/lease records only from a permitted observed transition.

    A renewal retains the original write timestamp. A recovered pending write
    never gets a later deadline than its original intent plus five minutes.
    Recovery of a tariff no longer represented by current verified quotes can
    identify ownership but produces an already expired lease for fallback.
    Persist both records under the caller's shared control lock.
    """
    clock = _instant(now)
    if clock is None:
        raise ValueError('aware_verification_time_required')
    result = {'schema_version': 1, 'verified': False, 'reason_codes': [],
        'ownership': None, 'lease': None, 'recovery_only': False,
        'application_acknowledgment_known': False, 'control_requests': 0}
    if not isinstance(state, dict):
        result['reason_codes'] = ['state_required']
        return result
    working = dict(state)
    if pending is not None:
        working['pending'] = pending
    decision = decide_cycle(working, now=clock)
    result['decision_action'] = decision['action']
    action = decision['action']
    if action not in {'renew_lease', 'enable_savings', 'reconcile_pending'}:
        result['reason_codes'] = ['lease_commit_not_permitted', *decision['reason_codes']]
        return result
    if publication_kind not in {'renewal', 'reconciliation', 'publication'}:
        result['reason_codes'] = ['publication_kind_invalid']
        return result
    recovery = action == 'reconcile_pending'
    if not recovery and publication_kind != 'renewal':
        result['reason_codes'] = ['new_publication_requires_pending_proof']
        return result
    policy = working['policy']
    publisher_pid = working.get('publisher_pid')
    if type(publisher_pid) is not int or not 0 < publisher_pid < 2**31:
        result['reason_codes'] = ['publisher_process_identity_required']
        return result
    site = working['energy_site_id']
    target = working['current_tariff_hash']
    keep = _keep(working['preserve_settings'])
    metadata = _source_metadata(working)
    deadline_text = proposed_lease_until(working, now=clock)
    deadline = _instant(deadline_text)
    original_kind = None
    acknowledged = False
    if recovery:
        intent = working['pending']
        created = _instant(intent['created_at'])
        first, second = intent['matching_readbacks'][-2:]
        first_at = _instant(first['observed_at'])
        completed = _instant(intent.get('http_completed_at'))
        written_at = (completed if completed is not None
            and created <= completed <= first_at else first_at)
        upper_bound = written_at is first_at
        acknowledged = intent.get('write_accepted') is True
        recovery_only = (decision['expired_recovery_only']
            or working.get('candidate_tariff_hash') != target or deadline is None
            or not _watchdog_ready(working, clock)
            or policy.get('fallback_approved') is not True
            or policy.get('frozen_assumption_approved') is not True)
        original_deadline = created + MAX_LEASE
        deadline = min(original_deadline, deadline) if deadline is not None else original_deadline
        if recovery_only:
            deadline = min(deadline, clock)
        recovery_only = recovery_only or deadline <= clock
        kind = 'acknowledged_write' if acknowledged else 'verified_pending_transition'
        evidence = {'baseline_tariff_hash': intent['baseline_hash'],
            'http_status': 200, 'application_acknowledgment_known': acknowledged,
            'matching_readbacks': 2,
            'first_matching_readback_at': first_at.isoformat(),
            'second_matching_readback_at': _instant(second['observed_at']).isoformat(),
            'intent_created_at': created.isoformat()}
    else:
        owner = working['ownership']
        written_at = _instant(owner['written_at'])
        upper_bound = owner.get('written_at_is_upper_bound') is True
        recovery_only = False
        kind = 'verified_owned_lease_renewal'
        original_kind = owner.get('publication_kind') if owner.get('publication_kind') in {
            'acknowledged_write', 'verified_pending_transition',
            'verified_owned_lease_renewal', 'supervised_transition_reconciliation'} else None
        acknowledged = owner.get('application_acknowledgment_known') is True
        evidence = {'renewal_without_post': True,
            'application_acknowledgment_known': acknowledged}
    if deadline is None or written_at is None:
        result['reason_codes'] = ['lease_deadline_or_write_time_unknown']
        return result
    generation = policy.get('generation')
    common = {'schema_version': 1, 'verified_publisher_write': True,
        'energy_site_id': site, 'tariff_semantic_hash': target,
        'last_verified_tariff_hash': target, 'policy_generation': generation,
        'preserve_settings': keep}
    ownership = {**common, 'written_at': written_at.isoformat(),
        'written_at_is_upper_bound': upper_bound,
        'last_verified_at': clock.isoformat(), 'publication_kind': kind,
        'application_acknowledgment_known': acknowledged,
        'source': metadata,
        'frozen_assumption_approved': policy.get('frozen_assumption_approved') is True,
        'grid_charging_state_known': type(keep['grid_charging']) is bool,
        'verification_evidence': evidence}
    if original_kind is not None:
        ownership['previous_publication_kind'] = original_kind
    lease = {**common, 'publisher_owns_installed_tariff': True,
        'intended_tariff_hash': target, 'guard_lease_trusted': True,
        'issued_at': clock.isoformat(), 'guard_lease_until': deadline.isoformat(),
        'publisher_pid': publisher_pid, 'recovery_only': recovery_only,
        'live_last_success': _instant(working['live_last_success']).isoformat(),
        'enrolled_last_success': _instant(working['enrolled_last_success']).isoformat(),
        **metadata}
    result.update(verified=True, ownership=ownership, lease=lease,
        recovery_only=recovery_only, application_acknowledgment_known=acknowledged,
        reason_codes=['verified_expired_recovery_only' if recovery_only
            else 'verified_pending_transition' if recovery else 'verified_owned_renewal'])
    return result


def record_mode_observation(journal: dict, mode_intent: dict, snapshot: dict,
        now: datetime, policy: dict) -> dict:
    """Recognise a previously authorised mode command from actual gateway state.

    Ordinary manual mode observations are never adopted as controller actions.
    The intent must have been persisted before dispatch. A network-uncertain
    dispatch can be reconciled with a fresh matching gateway observation; its
    application acknowledgement is not invented. This records evidence only.
    """
    clock = _instant(now)
    if clock is None:
        raise ValueError('aware_verification_time_required')
    result = {'schema_version': 1, 'verified': False, 'reason_codes': [],
        'journal': None, 'control_requests': 0}
    if not all(isinstance(item, dict) for item in (journal, mode_intent, snapshot, policy)):
        result['reason_codes'] = ['mode_records_required']
        return result
    intent = mode_intent
    created = _instant(intent.get('created_at'))
    observed = _instant(snapshot.get('observed_at', snapshot.get('live_last_success')))
    target, controller = intent.get('target'), intent.get('controller')
    site = snapshot.get('energy_site_id')
    generation = policy.get('generation')
    reasons = result['reason_codes']
    if (intent.get('authorized') is not True or intent.get('durable') is not True
        or intent.get('dispatch_started') is not True
        or not (intent.get('command_sent') is True or intent.get('command_sent') is None)
        or intent.get('write_accepted') is False or intent.get('explicit_rejection') is True):
        reasons.append('authorised_durable_dispatch_required')
    if (target not in {'autonomous', 'self_consumption'}
        or controller not in {'publisher', 'watchdog'}
        or intent.get('from_mode') not in {'autonomous', 'self_consumption'}
        or intent.get('from_mode') == target
        or (controller == 'watchdog' and target != 'self_consumption')):
        reasons.append('mode_intent_invalid')
    if target == 'autonomous' and policy.get('manage_savings') is not True:
        reasons.append('savings_management_not_authorised')
    if (not _site(site) or policy.get('energy_site_id') != site
        or intent.get('energy_site_id') != site
        or intent.get('policy_generation') != generation):
        reasons.append('mode_identity_or_policy_mismatch')
    if (not _hash(snapshot.get('current_tariff_hash'))
        or snapshot.get('current_tariff_hash') != intent.get('tariff_semantic_hash')):
        reasons.append('mode_tariff_changed_or_unknown')
    if (created is None or observed is None or not created <= observed <= clock
        or not _fresh(observed, LIVE_MAX_AGE, clock)
        or not _fresh(snapshot.get('live_last_success'), LIVE_MAX_AGE, clock)
        or snapshot.get('mode_source') != 'local_gateway_TEDAPI'
        or snapshot.get('mode') != target):
        reasons.append('matching_gateway_mode_observation_required')
    if (snapshot.get('battery_enrolled') is not False
        or not _fresh(snapshot.get('enrolled_last_success'), ENROLLMENT_MAX_AGE, clock)
        or snapshot.get('energy_commands_granted') is not True):
        reasons.append('mode_enrollment_or_permission_unverified')
    if (not _protected(intent.get('preserve_settings'))
        or not _protected(snapshot.get('preserve_settings'))
        or intent.get('preserve_settings') != snapshot.get('preserve_settings')):
        reasons.append('mode_protected_settings_changed_or_unknown')
    if reasons:
        return result
    updated = deepcopy(journal)
    updated['last_controlled_mode'] = target
    updated['last_mode_action'] = {'controller': controller, 'target': target,
        'verified': True, 'verified_at': observed.isoformat(),
        'energy_site_id': site, 'tariff_semantic_hash': snapshot['current_tariff_hash'],
        'policy_generation': generation,
        'command_sent': intent.get('command_sent'),
        'application_acknowledgment_known': intent.get('write_accepted') is True}
    if target == 'autonomous':
        updated['bootstrap_consumed'] = True
    result.update(verified=True, reason_codes=['matching_authorised_gateway_mode'], journal=updated)
    return result
