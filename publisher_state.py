"""Pure prerequisites and ownership/lease records for one controlled publish.

No files, credentials, PIDs, processes or services are accessed here. The caller
must supply observations from real checks: candidate validity/price comparison,
approved fallback/frozen assumptions, PID liveness and the separate watchdog's
actual heartbeat. This module never creates a watchdog heartbeat or enables a
policy. Every permission flag requires the actual boolean True.

``plan_publish`` consumes common guard fields plus:
  energy_site_id, publisher_pid, expected_baseline_hash, candidate_tariff_hash,
  candidate_valid, candidate_prices_verified, fallback_approved,
  frozen_assumption_approved, live_publication_enabled,
  preserve_settings_before, publisher_owns_installed_tariff, ownership,
  watchdog={continuous, armed, last_cycle_at, pid, pid_alive}.

``verified_publish_records`` additionally consumes:
  write_acknowledged (actual True for POST, False for a no-POST renewal),
  write_acknowledged_at (required for POST), fresh_readback_tariff_hash,
  preserve_settings_after, and readback={energy_site_id, observed_at,
  live_last_success, mode, battery_enrolled, enrolled_last_success,
  energy_commands_granted}. If present, readback.current_tariff_hash and
  readback.preserve_settings must agree with the explicit readback arguments.

Initial publication cannot claim ownership. A same-hash no-POST renewal needs
an existing verified ownership record plus fresh readback. New ownership is
produced only after an acknowledged write and matching post-write readback.
These are returned records, not persisted files or atomic Tesla transactions.
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
MAX_LEASE = timedelta(minutes=3)
MIN_LEASE = timedelta(seconds=30)
HASH = re.compile(r"[0-9a-f]{64}\Z")
EXPORT_RULES = frozenset({"battery_ok", "pv_only", "never"})


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


def _site(value: Any) -> bool:
    return type(value) is int and 0 < value < 2**63


def _pid(value: Any) -> bool:
    return type(value) is int and 0 < value < 2**31


def _preserve(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    reserve, grid, export = (value.get(key) for key in (
        "backup_reserve", "grid_charging", "export_permission"))
    if (isinstance(reserve, bool) or not isinstance(reserve, (int, float))
        or not 0 <= reserve <= 100 or not math.isfinite(reserve)
        or not (type(grid) is bool or (type(grid) is str and grid == "not_reported"))
        or not isinstance(export, str) or export not in EXPORT_RULES):
        return None
    return {"backup_reserve": reserve, "grid_charging": grid, "export_permission": export}


def _owned(state: dict, *, now: datetime) -> bool:
    ownership = state.get("ownership")
    if (state.get("publisher_owns_installed_tariff") is not True
        or not isinstance(ownership, dict)
        or ownership.get("verified_publisher_write") is not True
        or ownership.get("energy_site_id") != state.get("energy_site_id")
        or not _site(ownership.get("energy_site_id"))
        or not _hash(ownership.get("tariff_semantic_hash"))
        or ownership.get("tariff_semantic_hash") != state.get("current_tariff_hash")):
        return False
    written = _instant(ownership.get("written_at"))
    verified = _instant(ownership.get("last_verified_at"))
    return bool(written is not None and verified is not None and written <= verified <= now)


def plan_publish(state: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    """Return a bounded lease proposal only after all actual checks succeed."""
    clock = _instant(now)
    reasons: list[str] = []
    result: dict[str, Any] = {
        "schema_version": 1,
        "assessed_at": clock.isoformat() if clock is not None else None,
        "allow_write": False,
        "write_required": True,
        "allow_renewal_without_post": False,
        "proposed_guard_lease_until": None,
        "latest_safe_deadline": None,
        "reason_codes": reasons,
        "ownership_created": False,
        "control_requests": 0,
    }
    if clock is None:
        reasons.append("assessment_clock_invalid")
        return result
    if not isinstance(state, dict):
        state = {}

    def add(code):
        if code not in reasons:
            reasons.append(code)

    def fresh(value, name, maximum):
        timestamp = _instant(value)
        if timestamp is None:
            add(name + "_unknown")
        elif timestamp > clock:
            add(name + "_in_future")
        elif clock - timestamp >= maximum:
            add(name + "_stale")
        else:
            return timestamp
        return None

    source_success = fresh(state.get("source_last_success"), "source_last_success", SOURCE_MAX_AGE)
    captured = fresh(state.get("source_captured_at"), "source_captured_at", SOURCE_MAX_AGE)
    enrolled_at = fresh(state.get("enrolled_last_success"), "enrolled_last_success", ENROLLMENT_MAX_AGE)
    live_at = fresh(state.get("live_last_success"), "live_last_success", LIVE_MAX_AGE)
    if source_success is not None and captured is not None and source_success > captured:
        add("source_success_after_capture")
    valid_from = _instant(state.get("known_valid_from"))
    valid_until = _instant(state.get("known_valid_until"))
    if valid_from is None or valid_until is None or valid_until <= valid_from:
        add("quoted_window_invalid")
    elif not valid_from <= clock < valid_until:
        add("quoted_window_not_current")
    for key in ("candidate_valid", "candidate_prices_verified", "fallback_approved",
                "frozen_assumption_approved", "live_publication_enabled", "energy_commands_granted"):
        if state.get(key) is not True:
            add(key + "_required")
    if state.get("battery_enrolled") is not False:
        add("optimise_not_confirmed_unenrolled")
    if state.get("mode") != "autonomous":
        add("savings_mode_not_confirmed")
    if not _site(state.get("energy_site_id")):
        add("energy_site_identity_invalid")
    for key in ("current_tariff_hash", "expected_baseline_hash", "candidate_tariff_hash"):
        if not _hash(state.get(key)):
            add(key + "_invalid")
    if state.get("current_tariff_hash") != state.get("expected_baseline_hash"):
        add("installed_tariff_changed")
    if _preserve(state.get("preserve_settings_before")) is None:
        add("protected_settings_unknown")

    watchdog = state.get("watchdog")
    if not isinstance(watchdog, dict):
        watchdog = {}
    for key in ("continuous", "armed", "pid_alive"):
        if watchdog.get(key) is not True:
            add("watchdog_" + key + "_required")
    fresh(watchdog.get("last_cycle_at"), "watchdog_cycle", WATCHDOG_MAX_AGE)
    if not _pid(state.get("publisher_pid")) or not _pid(watchdog.get("pid")):
        add("process_identity_invalid")
    elif state["publisher_pid"] == watchdog["pid"]:
        add("watchdog_not_separate_process")

    deadlines = [clock + MAX_LEASE]
    for timestamp, maximum in ((source_success, SOURCE_MAX_AGE), (captured, SOURCE_MAX_AGE),
                               (enrolled_at, ENROLLMENT_MAX_AGE), (live_at, LIVE_MAX_AGE)):
        if timestamp is not None:
            deadlines.append(timestamp + maximum)
    if valid_until is not None:
        deadlines.append(valid_until)
    deadline = min(deadlines)
    result["latest_safe_deadline"] = deadline.isoformat()
    if deadline - clock < MIN_LEASE:
        add("insufficient_lease_headroom")
    elif not reasons:
        result["proposed_guard_lease_until"] = deadline.isoformat()

    owned = _owned(state, now=clock)
    same_target = (_hash(state.get("candidate_tariff_hash"))
                   and state.get("candidate_tariff_hash") == state.get("current_tariff_hash"))
    result["ownership_verified_for_renewal"] = owned
    result["write_required"] = not (owned and same_target)
    result["allow_write"] = not reasons
    result["allow_renewal_without_post"] = bool(not reasons and not result["write_required"])
    return result


def verified_publish_records(state: dict[str, Any], *, verified_at: datetime) -> dict[str, Any]:
    """Refuse ownership/lease records until post-write or owned-renewal proof."""
    clock = _instant(verified_at)
    reasons: list[str] = []
    result: dict[str, Any] = {
        "schema_version": 1, "verified": False, "reason_codes": reasons,
        "ownership": None, "lease": None, "control_requests": 0,
    }
    if clock is None:
        reasons.append("verification_clock_invalid")
        return result
    if not isinstance(state, dict):
        state = {}
    plan = plan_publish(state, now=clock)
    result["plan"] = plan
    if not plan["allow_write"]:
        reasons.extend(plan["reason_codes"])
        return result

    def add(code):
        if code not in reasons:
            reasons.append(code)

    target = state["candidate_tariff_hash"]
    fresh_hash = state.get("fresh_readback_tariff_hash")
    if not _hash(fresh_hash) or fresh_hash != target:
        add("candidate_readback_mismatch")
    readback = state.get("readback")
    if not isinstance(readback, dict):
        readback = {}
    observed = _instant(readback.get("observed_at"))
    live_at = _instant(readback.get("live_last_success"))
    enrolled_at = _instant(readback.get("enrolled_last_success"))
    if observed is None or not timedelta(0) <= clock - observed < LIVE_MAX_AGE:
        add("readback_observation_not_fresh")
    if live_at is None or not timedelta(0) <= clock - live_at < LIVE_MAX_AGE:
        add("readback_live_status_not_fresh")
    if enrolled_at is None or not timedelta(0) <= clock - enrolled_at < ENROLLMENT_MAX_AGE:
        add("readback_enrollment_not_fresh")
    if (observed is not None and live_at is not None and live_at > observed) or (
        observed is not None and enrolled_at is not None and enrolled_at > observed):
        add("readback_times_inconsistent")
    if readback.get("energy_site_id") != state["energy_site_id"] or not _site(readback.get("energy_site_id")):
        add("readback_site_mismatch")
    if readback.get("mode") != "autonomous":
        add("readback_mode_changed")
    if readback.get("battery_enrolled") is not False:
        add("readback_optimise_not_confirmed_unenrolled")
    if readback.get("energy_commands_granted") is not True:
        add("readback_energy_commands_unavailable")
    if "current_tariff_hash" in readback and readback["current_tariff_hash"] != fresh_hash:
        add("readback_hash_fields_disagree")
    before = _preserve(state.get("preserve_settings_before"))
    after = _preserve(state.get("preserve_settings_after"))
    if before is None or after is None or before != after:
        add("protected_settings_changed_or_unknown")
    if "preserve_settings" in readback and _preserve(readback["preserve_settings"]) != after:
        add("readback_protected_fields_disagree")

    acknowledged = state.get("write_acknowledged")
    if acknowledged is True:
        written_at = _instant(state.get("write_acknowledged_at"))
        captured = _instant(state.get("source_captured_at"))
        if (written_at is None or observed is None or live_at is None or captured is None
            or not captured <= written_at <= live_at <= observed <= clock
            or clock - written_at >= LIVE_MAX_AGE):
            add("post_write_readback_order_unverified")
        publication_kind = "acknowledged_write"
    elif acknowledged is False and plan["allow_renewal_without_post"]:
        written_at = _instant(state["ownership"]["written_at"])
        publication_kind = "verified_owned_lease_renewal"
    else:
        written_at = None
        publication_kind = "unverified"
        add("acknowledged_write_or_owned_renewal_required")
    if reasons:
        return result

    # Reassess with fresh postflight observations. The current installed hash is
    # deliberately retained as the preflight baseline: this only clips the lease
    # to the fresh live/enrollment validity windows, never blesses drift.
    lease_state = dict(state, live_last_success=live_at, enrolled_last_success=enrolled_at)
    lease_plan = plan_publish(lease_state, now=clock)
    if not lease_plan["allow_write"]:
        reasons.extend(lease_plan["reason_codes"])
        return result
    metadata = {key: _instant(state[key]).isoformat() for key in (
        "source_last_success", "source_captured_at", "known_valid_from", "known_valid_until")}
    ownership = {
        "schema_version": 1, "verified_publisher_write": True,
        "energy_site_id": state["energy_site_id"], "tariff_semantic_hash": target,
        "written_at": written_at.isoformat(), "last_verified_at": clock.isoformat(),
        "last_verified_tariff_hash": target, "publication_kind": publication_kind,
        "preserve_settings": dict(after),
        "grid_charging_state_known": type(after["grid_charging"]) is bool,
        "source": dict(metadata), "frozen_assumption_approved": True,
    }
    lease = {
        "schema_version": 1, "verified_publisher_write": True,
        "publisher_owns_installed_tariff": True,
        "energy_site_id": state["energy_site_id"], "tariff_semantic_hash": target,
        "last_verified_tariff_hash": target, "intended_tariff_hash": target,
        "issued_at": clock.isoformat(), "publisher_pid": state["publisher_pid"],
        "guard_lease_until": lease_plan["proposed_guard_lease_until"],
        "guard_lease_trusted": True, "preserve_settings": dict(after),
        "live_last_success": live_at.isoformat(), "enrolled_last_success": enrolled_at.isoformat(),
        **metadata,
    }
    result.update(verified=True, ownership=ownership, lease=lease,
                  publication_kind=publication_kind, grid_charging_state_known=ownership["grid_charging_state_known"])
    return result


def reconcile_supervised_transition(state: dict[str, Any], receipt: dict[str, Any],
                                    *, verified_at: datetime) -> dict[str, Any]:
    """Certify an already observed supervised write with unknown result body.

    This is not another publication or a lease renewal. It never turns an
    unknown application acknowledgment into True, and never requests a resend.
    It needs a documented supervised approval, an HTTP-200 dispatch from a
    different known preflight hash, a matching first postflight, and a second
    independently refreshed readback. Explicit application rejection always
    refuses certification. The lease cannot outlast original dispatch +3min.

    State requires supervised_live_approval=True, live_approval_id matching the
    receipt, write_acknowledged=False, independent_readback_verified=True,
    fresh_readback_tariff_hash, readback and the ordinary price/controller/
    watchdog/assumption prerequisites. Current publication may be disabled:
    that prevents new POSTs, not reconciliation of the supervised past POST.
    An already-expired dispatch deadline yields an already-expired trusted lease
    for guarded recovery, never three fresh minutes of tariff operation.
    """
    clock = _instant(verified_at)
    reasons: list[str] = []
    result: dict[str, Any] = {
        "schema_version": 1, "verified": False, "reason_codes": reasons,
        "ownership": None, "lease": None, "control_requests": 0,
        "pricing_post_repeated": False, "write_acknowledged": False,
        "application_acknowledgment_known": False,
    }
    if clock is None:
        reasons.append("verification_clock_invalid")
        return result
    if not isinstance(state, dict):
        state = {}
    if not isinstance(receipt, dict):
        receipt = {}

    def add(code):
        if code not in reasons:
            reasons.append(code)

    approval = state.get("live_approval_id")
    if (state.get("supervised_live_approval") is not True
        or not isinstance(approval, str)
        or re.fullmatch(r"[A-Za-z0-9_.:-]{4,160}", approval) is None
        or receipt.get("live_approval_id") != approval):
        add("supervised_approval_not_verified")
    if state.get("write_acknowledged") is not False:
        add("application_acknowledgment_not_unknown")
    if state.get("independent_readback_verified") is not True:
        add("independent_readback_not_verified")
    if type(state.get("live_publication_enabled")) is not bool:
        add("publication_policy_state_unknown")
    if type(receipt.get("http_status")) is not int or receipt.get("http_status") != 200:
        add("supervised_http200_required")
    if receipt.get("command_sent") is not True:
        add("supervised_dispatch_not_verified")
    response = receipt.get("response")
    explicit_failure = (receipt.get("explicit_failure") is True
        or receipt.get("write_accepted") is False
        or receipt.get("result") is False or receipt.get("write_result") is False
        or (isinstance(response, dict) and response.get("result") is False))
    if explicit_failure:
        add("explicit_application_rejection")
    if explicit_failure or receipt.get("write_accepted") is not None or receipt.get("code") is not None:
        result["application_acknowledgment_known"] = True
    if ("write_accepted" not in receipt or "code" not in receipt
        or receipt.get("write_accepted") is not None or receipt.get("code") is not None):
        add("unknown_application_result_required")

    before_hash = receipt.get("preflight_tariff_hash")
    target = receipt.get("candidate_tariff_hash")
    if not _hash(before_hash) or not _hash(target) or before_hash == target:
        add("distinct_preflight_transition_not_verified")
    if state.get("expected_baseline_hash") != before_hash:
        add("approved_preflight_baseline_mismatch")
    if state.get("candidate_tariff_hash") != target:
        add("approved_candidate_mismatch")
    if receipt.get("first_readback_tariff_hash") != target:
        add("first_readback_candidate_mismatch")
    if state.get("fresh_readback_tariff_hash") != target:
        add("independent_readback_candidate_mismatch")
    site = state.get("energy_site_id")
    if not _site(site) or receipt.get("energy_site_id") != site or not _site(receipt.get("energy_site_id")):
        add("receipt_site_mismatch")

    created = _instant(receipt.get("created_at"))
    first_at = _instant(receipt.get("first_readback_at"))
    readback = state.get("readback")
    if not isinstance(readback, dict):
        readback = {}
    # Assess the immutable preflight baseline with the second readback's actual
    # controller observations. Do not adopt its installed target as a baseline.
    gate_state = dict(state, current_tariff_hash=before_hash)
    for key in ("live_last_success", "enrolled_last_success", "battery_enrolled",
                "mode", "energy_commands_granted"):
        gate_state[key] = readback.get(key)
    plan = plan_publish(gate_state, now=clock)
    # These two gates govern a NEW write, not certification/recovery of the past
    # supervised dispatch. All other freshness/approval/health gates still hold.
    recovery_exceptions = {"live_publication_enabled_required", "insufficient_lease_headroom"}
    for code in plan["reason_codes"]:
        if code not in recovery_exceptions:
            add(code)
    observed = _instant(readback.get("observed_at"))
    live_at = _instant(readback.get("live_last_success"))
    enrolled_at = _instant(readback.get("enrolled_last_success"))
    captured = _instant(state.get("source_captured_at"))
    if (created is None or first_at is None or observed is None or captured is None
        or not captured <= created <= first_at < observed <= clock):
        add("supervised_transition_order_unverified")
    if observed is None or not timedelta(0) <= clock - observed < LIVE_MAX_AGE:
        add("independent_readback_not_fresh")
    if (live_at is None or first_at is None or observed is None
        or not first_at <= live_at <= observed
        or not timedelta(0) <= clock - live_at < LIVE_MAX_AGE):
        add("independent_live_status_not_fresh")
    if (enrolled_at is None or observed is None or enrolled_at > observed
        or not timedelta(0) <= clock - enrolled_at < ENROLLMENT_MAX_AGE):
        add("independent_enrollment_not_fresh")
    if readback.get("energy_site_id") != site or not _site(readback.get("energy_site_id")):
        add("independent_readback_site_mismatch")
    if readback.get("current_tariff_hash") != target:
        add("independent_tariff_changed")
    if readback.get("mode") != "autonomous":
        add("independent_mode_changed")
    if readback.get("battery_enrolled") is not False:
        add("independent_optimise_not_confirmed_unenrolled")
    if readback.get("energy_commands_granted") is not True:
        add("independent_energy_commands_unavailable")

    preserve = _preserve(state.get("preserve_settings_before"))
    representations = [
        _preserve(receipt.get("preflight_preserve_settings")),
        _preserve(receipt.get("first_readback_preserve_settings")),
        _preserve(state.get("preserve_settings_after")),
        _preserve(readback.get("preserve_settings")),
    ]
    if preserve is None or any(value is None or value != preserve for value in representations):
        add("supervised_protected_settings_changed_or_unknown")
    if reasons:
        return result

    # Reclip with the second readback's actual fresh observation windows while
    # retaining original source/window and original dispatch as the hard cap.
    deadlines = [created + MAX_LEASE, _instant(state["known_valid_until"]),
        _instant(state["source_last_success"]) + SOURCE_MAX_AGE,
        captured + SOURCE_MAX_AGE,
        enrolled_at + ENROLLMENT_MAX_AGE, live_at + LIVE_MAX_AGE]
    until = min(deadlines)
    expired = until <= clock
    metadata = {key: _instant(state[key]).isoformat() for key in (
        "source_last_success", "source_captured_at", "known_valid_from", "known_valid_until")}
    evidence = {
        "publication_kind": "supervised_verified_http200_transition",
        "verification_basis": "distinct_preflight_and_two_matching_postflight_observations",
        "live_approval_id": approval, "application_acknowledgment_known": False,
        "write_acknowledged": False, "write_accepted": None, "write_response_code": None,
        "write_http_status": 200, "dispatch_created_at": created.isoformat(),
        "first_readback_at": first_at.isoformat(), "independent_readback_at": observed.isoformat(),
        "prior_tariff_semantic_hash": before_hash, "pricing_post_repeated": False,
    }
    ownership = {
        "schema_version": 1, "verified_publisher_write": True,
        "energy_site_id": site, "tariff_semantic_hash": target,
        "last_verified_tariff_hash": target,
        "written_at": first_at.isoformat(), "written_at_is_upper_bound": True,
        "last_verified_at": clock.isoformat(), "preserve_settings": dict(preserve),
        "grid_charging_state_known": type(preserve["grid_charging"]) is bool,
        "source": dict(metadata), "frozen_assumption_approved": True, **evidence,
    }
    lease = {
        "schema_version": 1, "verified_publisher_write": True,
        "publisher_owns_installed_tariff": True,
        "energy_site_id": site, "tariff_semantic_hash": target,
        "last_verified_tariff_hash": target, "intended_tariff_hash": target,
        "issued_at": clock.isoformat(), "publisher_pid": state["publisher_pid"],
        "guard_lease_until": until.isoformat(), "guard_lease_trusted": True,
        "expiry_already_passed": expired, "preserve_settings": dict(preserve),
        "live_last_success": live_at.isoformat(), "enrolled_last_success": enrolled_at.isoformat(),
        **metadata, **evidence,
    }
    result.update(verified=True, ownership=ownership, lease=lease,
        expiry_already_passed=expired, recovery_only=expired,
        application_acknowledgment_known=False,
        grid_charging_state_known=ownership["grid_charging_state_known"])
    return result
