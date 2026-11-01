"""Durable, private mode-intent records shared by the recurring processes.

Callers hold STATE_DIR/.control.lock. A recorded dispatch is never replayed;
uncertain outcomes need an independent actual-mode observation or operator review.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets

from config import STATE_DIR
from runtime import private_document
from recurring_records import record_mode_observation
from recurring_state import _fresh, _hash, _instant, _protected, _site

OUTPUT = STATE_DIR / 'output'
UTC = timezone.utc


def optional_private(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('private_record_symlink_rejected')
    return private_document(path) if path.exists() else {}


def _sync_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_private_json(path, value):
    """Atomically replace a private record and sync both data and directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (path.parent.is_symlink() or path.parent.stat().st_uid != os.getuid()
        or path.is_symlink()):
        raise ValueError('private_record_directory_invalid')
    temporary = path.parent / ('.' + path.name + '.' + str(os.getpid()) + '.' + secrets.token_hex(6))
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, 'w') as handle:
            json.dump(value, handle, allow_nan=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_intent():
    path = OUTPUT / 'mode-intent.json'
    if path.is_symlink():
        raise ValueError('mode_intent_symlink_rejected')
    path.unlink(missing_ok=True)
    _sync_directory(OUTPUT)


def record_mode_attempt(context, *, controller, policy):
    """Fsync a bounded, authorised intent immediately before transport GO."""
    try:
        if controller not in {'publisher', 'watchdog'} or not isinstance(context, dict) or not isinstance(policy, dict):
            return False
        if optional_private(OUTPUT / 'mode-intent.json'):
            return False
        journal = optional_private(OUTPUT / 'recurring-journal.json')
        if journal.get('pause_latch'):
            return False
        target = context.get('target_mode')
        action = context.get('action')
        origin = context.get('from_mode')
        site = context.get('energy_site_id')
        now = datetime.now(UTC)
        generation = policy.get('generation')
        if (not isinstance(generation, str) or not generation or len(generation) > 128
            or not _site(site) or policy.get('energy_site_id') != site
            or not _hash(context.get('current_tariff_hash'))
            or not _protected(context.get('preserve_settings'))
            or context.get('battery_enrolled') is not False
            or not _fresh(context.get('enrolled_last_success'), timedelta(seconds=30), now)):
            return False
        if controller == 'watchdog':
            if (action != 'fallback' or target != 'self_consumption' or origin != 'autonomous'
                or policy.get('fallback_approved') is not True):
                return False
        elif (action != 'enable_savings' or target != 'autonomous' or origin != 'self_consumption'
            or policy.get('enabled') is not True or policy.get('manage_savings') is not True):
            return False
        intent = {'schema_version': 1, 'kind': 'mode', 'authorized': True,
            'durable': True, 'dispatch_started': True, 'command_sent': None,
            'controller': controller, 'target': target, 'from_mode': origin,
            'energy_site_id': site, 'tariff_semantic_hash': context['current_tariff_hash'],
            'policy_generation': generation, 'created_at': now.isoformat(),
            'preserve_settings': dict(context['preserve_settings']),
            'write_accepted': None, 'explicit_rejection': False}
        durable_private_json(OUTPUT / 'mode-intent.json', intent)
        return True
    except (ValueError, OSError, TypeError):
        return False


def complete_mode_attempt(result):
    """Record fixed outcome fields; never save provider messages or tokens."""
    intent = optional_private(OUTPUT / 'mode-intent.json')
    if not intent:
        return {'pending': False, 'reason_codes': ['no_mode_intent']}
    value = result if isinstance(result, dict) else {}
    sent = value.get('command_sent')
    accepted = value.get('accepted')
    status = value.get('mode_http_status')
    completed = _instant(value.get('mode_http_completed_at'))
    intent.update(command_sent=sent if type(sent) is bool else None,
        write_accepted=accepted if type(accepted) is bool else None,
        explicit_rejection=value.get('explicit_rejection') is True or accepted is False,
        http_status=status if type(status) is int and 100 <= status <= 599 else None,
        http_completed_at=completed.isoformat() if completed is not None else None,
        completed_recorded_at=datetime.now(UTC).isoformat())
    durable_private_json(OUTPUT / 'mode-intent.json', intent)
    if sent is False:
        durable_private_json(OUTPUT / 'mode-intent-last.json', intent)
        _remove_intent()
        return {'pending': False, 'verified': False, 'reason_codes': ['mode_dispatch_aborted']}
    return {'pending': True, 'verified': False, 'reason_codes': ['mode_observation_required']}


def reconcile_mode(snapshot, *, policy, now):
    """Clear a mode intent only on explicit no-dispatch or verified observation."""
    intent = optional_private(OUTPUT / 'mode-intent.json')
    if not intent:
        return {'pending': False, 'verified': False, 'reason_codes': ['no_mode_intent']}
    journal = optional_private(OUTPUT / 'recurring-journal.json')
    if intent.get('command_sent') is False:
        durable_private_json(OUTPUT / 'mode-intent-last.json', intent)
        _remove_intent()
        return {'pending': False, 'verified': False, 'reason_codes': ['mode_dispatch_aborted']}
    if intent.get('explicit_rejection') is True or intent.get('write_accepted') is False:
        if not journal.get('pause_latch'):
            journal['pause_latch'] = {'kind': 'manual', 'reason': 'mode_command_rejected',
                'created_at': _instant(now).isoformat()}
        durable_private_json(OUTPUT / 'recurring-journal.json', journal)
        durable_private_json(OUTPUT / 'mode-intent-last.json', intent)
        _remove_intent()
        return {'pending': False, 'verified': False, 'rejected': True,
            'reason_codes': ['mode_command_rejected_manual_review']}
    record = record_mode_observation(journal, intent, snapshot, now, policy)
    if record.get('verified') is True:
        durable_private_json(OUTPUT / 'recurring-journal.json', record['journal'])
        intent.update(observation_verified=True, observed_at=snapshot['observed_at'])
        durable_private_json(OUTPUT / 'mode-intent-last.json', intent)
        _remove_intent()
        return {'pending': False, 'verified': True, 'reason_codes': record['reason_codes']}
    return {'pending': True, 'verified': False, 'reason_codes': record['reason_codes']}
