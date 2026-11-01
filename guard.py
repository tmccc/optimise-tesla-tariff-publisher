"""Pure proposed policy for a temporary Optimise-to-Tesla tariff publisher.

This module does not run a watchdog, read a credential, use a network, or change
Tesla/Home Assistant settings. Its action is a proposal for a separately
authorised executor. Stopping uploads alone never expires a Tesla tariff.

Required input fields (timestamps may be aware datetimes or ISO 8601 strings):
  source_last_success, source_captured_at, known_valid_from, known_valid_until,
  live_last_success, battery_enrolled (actual bool), enrolled_last_success,
  energy_commands_granted (actual bool), mode (autonomous/self_consumption),
  current_tariff_hash, last_verified_tariff_hash, intended_tariff_hash,
  guard_lease_until, watchdog_verified (actual bool),
  fallback_policy='self_powered', fallback_approved (actual bool).

Fallback additionally requires publisher_owns_installed_tariff=True. The
executor may assert ownership only after storing a verified successful write;
equal current/previous hashes do not establish that this publisher owns a
pre-existing baseline. Ownership is not required for the initial publication.

``current_tariff_hash`` must match the *previously verified installed* hash.
The intended next hash may differ: that is normal when prices update. The
caller must validate the candidate's schema/rates separately before publishing.
The watchdog and approval flags are assertions by the caller, not evidence that
this module installed a watchdog or obtained permission.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Any

UTC = timezone.utc
SOURCE_MAX_AGE = timedelta(minutes=15)
ENROLLMENT_MAX_AGE = timedelta(minutes=45)
LIVE_MAX_AGE = timedelta(minutes=5)
HASH = re.compile(r"[0-9a-f]{64}\Z")


def _instant(value: Any) -> datetime | None:
    if isinstance(value, str):
        if len(value) > 80:
            return None
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
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


def evaluate_guard(state: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    """Evaluate prerequisites and propose an action; never execute that action.

    Any missing/invalid prerequisite blocks publication. A confirmed enrolled
    battery always requests handover and never proposes a mode change that
    could override E.ON. Unknown/stale enrolment requires human verification;
    absence of evidence is never interpreted as inactive SmartShift.
    """
    clock = _instant(now)
    if clock is None:
        raise ValueError("Guard assessment time must be timezone aware")
    if not isinstance(state, dict):
        state = {}
    reasons: list[str] = []
    operational: list[str] = []

    def add(code: str, *, operation: bool = True) -> None:
        if code not in reasons:
            reasons.append(code)
        if operation and code not in operational:
            operational.append(code)

    def fresh(key: str, maximum: timedelta) -> datetime | None:
        timestamp = _instant(state.get(key))
        if timestamp is None:
            add(key + "_unknown")
        elif timestamp > clock:
            add(key + "_in_future")
        elif clock - timestamp >= maximum:
            add(key + "_stale")
        else:
            return timestamp
        return None

    source_success = fresh("source_last_success", SOURCE_MAX_AGE)
    captured = fresh("source_captured_at", SOURCE_MAX_AGE)
    live_success = fresh("live_last_success", LIVE_MAX_AGE)
    enrolled_success = fresh("enrolled_last_success", ENROLLMENT_MAX_AGE)
    enrolled = state.get("battery_enrolled")
    confirmed_unenrolled = enrolled is False and enrolled_success is not None
    if enrolled is True:
        add("optimise_battery_enrolled")
    elif enrolled is not False:
        add("optimise_battery_enrollment_unknown")

    quoted_from = _instant(state.get("known_valid_from"))
    quoted_until = _instant(state.get("known_valid_until"))
    if quoted_from is None or quoted_until is None or quoted_until <= quoted_from:
        add("quoted_window_invalid")
    else:
        if clock < quoted_from:
            add("quoted_window_not_started")
        if clock >= quoted_until:
            add("quoted_window_expired")

    if captured is not None and source_success is not None and source_success > captured:
        add("source_success_after_capture")
    if state.get("energy_commands_granted") is not True:
        add("energy_commands_unavailable")
    mode = state.get("mode")
    if not isinstance(mode, str) or mode not in {"autonomous", "self_consumption"}:
        add("operation_mode_unknown")
    current_hash = state.get("current_tariff_hash")
    previous_hash = state.get("last_verified_tariff_hash")
    intended_hash = state.get("intended_tariff_hash")
    if not _hash(current_hash) or not _hash(previous_hash):
        add("installed_tariff_verification_missing")
    elif current_hash != previous_hash:
        add("installed_tariff_drift")
    if not _hash(intended_hash):
        add("intended_tariff_hash_invalid", operation=False)

    lease = _instant(state.get("guard_lease_until"))
    expiry_limits = [x for x in (
        quoted_until,
        source_success + SOURCE_MAX_AGE if source_success is not None else None,
        captured + SOURCE_MAX_AGE if captured is not None else None,
        live_success + LIVE_MAX_AGE if live_success is not None else None,
        enrolled_success + ENROLLMENT_MAX_AGE if enrolled_success is not None else None,
    ) if x is not None]
    deadline = min(expiry_limits) if expiry_limits else None
    if lease is None:
        add("guard_lease_unknown")
    elif lease <= clock:
        add("guard_lease_expired")
    elif deadline is None or lease > deadline:
        add("guard_lease_exceeds_safe_deadline")
    if state.get("watchdog_verified") is not True:
        add("independent_watchdog_unverified", operation=False)
    fallback_approved = (
        state.get("fallback_policy") == "self_powered"
        and state.get("fallback_approved") is True
    )
    owns_installed = state.get("publisher_owns_installed_tariff") is True
    if not fallback_approved:
        add("expiry_fallback_unapproved", operation=False)

    allow = not reasons
    # A changed tariff, unknown mode, or unavailable live readback can signal
    # another controller/human has taken over. Price expiry is not permission
    # to override those changes. A watchdog must get fresh ownership evidence
    # before proposing a mode-setting fallback.
    controller_verification_required = any(
        code == "operation_mode_unknown"
        or code.startswith("installed_tariff_")
        or code.startswith("live_last_success_")
        for code in reasons
    )
    if enrolled is True:
        action = "handover_to_optimise_without_control_writes"
    elif not confirmed_unenrolled:
        action = "pause_publishing_and_verify_controller"
    elif controller_verification_required:
        action = "pause_publishing_and_verify_controller"
    elif mode == "autonomous" and operational:
        if not owns_installed:
            add("publisher_ownership_unverified", operation=False)
            action = "request_operator_fallback"
        elif fallback_approved and state.get("energy_commands_granted") is True:
            action = "propose_self_powered_fallback"
        else:
            action = "request_operator_fallback"
    elif allow:
        action = "eligible_for_separately_authorised_publish"
    else:
        action = "complete_safety_prerequisites"

    return {
        "schema_version": 1,
        "evaluated_at": clock.isoformat(),
        "allow_publish": allow,
        "keep_savings_eligible": allow,
        "reason_codes": reasons,
        "proposed_action": action,
        "action_executed": False,
        "confirmed_optimise_unenrolled": confirmed_unenrolled,
        "publisher_owns_installed_tariff": owns_installed,
        "mode_change_permitted_by_policy": bool(
            action == "propose_self_powered_fallback"
            and confirmed_unenrolled and fallback_approved and owns_installed
            and state.get("energy_commands_granted") is True
        ),
        "latest_safe_deadline": deadline.isoformat() if deadline is not None else None,
        "independent_watchdog_required": True,
        "stopping_uploads_expires_tesla_tariff": False,
        "preserve_settings": ["backup_reserve", "grid_charging", "export_permission"],
        "candidate_schema_validation_required": True,
    }
