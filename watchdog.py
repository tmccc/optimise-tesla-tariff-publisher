"""Side-effect-free orchestration of a proposed Self-Powered fallback.

By default this only evaluates policy. It implements no credential handling,
network, file IO, timer, service or publisher-heartbeat renewal. An explicitly
enabled caller may inject an executor with this contract:

``read_snapshot()`` returns observational guard fields, ``preserve_settings``
and ``observed_at`` (the trusted host UTC time AFTER the reads completed).
``set_self_powered(expected_tariff_hash=..., preserve_settings=...)`` MUST
repeat a fresh preflight itself, including trusted lease/ownership state,
before its sole mode-changing request. That POST must contain ONLY the operation
mode field; protected settings are preconditions, never fields to write. It returns actual booleans
``preflight_verified`` and ``command_sent``. It must abort on concurrent
lease renewal, ownership loss, E.ON activation, mode/tariff/settings drift,
unknown inputs, or disabled live policy.

The check and Tesla POST are not atomic. The orchestrator's preflight and fresh
postflight reduce and expose races; they cannot guarantee compare-and-swap.
No failure ever triggers an automatic return to Savings.

The exact grid-charging value ``'not_reported'`` denotes an API-omitted optional
field. It is not False, True, or a guess about actual grid permission. A stable
omission verifies only the reported representation stayed omitted; it cannot
prove actual grid permission stayed unchanged. The mode-only executor contract
still forbids this fallback from writing any grid-permission field.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from typing import Any

from guard import evaluate_guard

UTC = timezone.utc
MAX_CYCLE_DURATION = timedelta(minutes=5)
PRESERVED_FIELDS = ("backup_reserve", "grid_charging", "export_permission")
EXPORT_RULES = frozenset({"battery_ok", "pv_only", "never"})
OBSERVATIONAL_FIELDS = (
    "live_last_success", "battery_enrolled", "enrolled_last_success",
    "energy_commands_granted", "mode", "current_tariff_hash",
)


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


def _preserve(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    reserve = value.get("backup_reserve")
    grid = value.get("grid_charging")
    export = value.get("export_permission")
    if (isinstance(reserve, bool) or not isinstance(reserve, (int, float))
        or not 0 <= reserve <= 100 or not math.isfinite(reserve)
        or not (type(grid) is bool or (type(grid) is str and grid == "not_reported"))
        or not isinstance(export, str) or export not in EXPORT_RULES):
        return None
    return {"backup_reserve": reserve, "grid_charging": grid, "export_permission": export}


def _snapshot_time(snapshot: Any, *, start: datetime, previous: datetime) -> datetime | None:
    if not isinstance(snapshot, dict):
        return None
    observed = _instant(snapshot.get("observed_at"))
    if observed is None or not previous <= observed <= start + MAX_CYCLE_DURATION:
        return None
    return observed


def _merge_observations(state: dict, snapshot: dict) -> dict:
    # Do not let an executor response replace approval, ownership or lease facts.
    merged = dict(state)
    for field in OBSERVATIONAL_FIELDS:
        merged[field] = snapshot.get(field)
    return merged


def evaluate_cycle(state: dict[str, Any], *, now: datetime, executor=None,
                   live_execution_enabled: bool = False) -> dict[str, Any]:
    """Evaluate one cycle, optionally executing only a guarded fallback.

    ``guard_lease_trusted=True`` is required in addition to the existing guard
    inputs. The trusted lease must parse as an aware timestamp and have expired.
    Unusable current prices must cause the publisher to invalidate its lease;
    the watchdog does not renew or invalidate the publisher's lease. Explicit publisher
    ownership must be backed by a stored verified write, not inferred from
    matching hashes of a pre-existing tariff.
    """
    clock = _instant(now)
    audit: dict[str, Any] = {
        "schema_version": 1,
        "evaluated_at": clock.isoformat() if clock is not None else None,
        "status": "observe_only",
        "live_execution_enabled": live_execution_enabled is True,
        "eligible_for_fallback": False,
        "reason_codes": [],
        "guard_action": None,
        "guard_reason_codes": [],
        "executor_called": False,
        "preflight_verified": False,
        "executor_preflight_attested": False,
        "command_attempted": False,
        "command_sent": False,
        "action_executed": False,
        "fallback_verified": False,
        "desired_state_verified": False,
        "control_requests": 0,
        "guard_lease_modified": False,
        "publisher_heartbeat_modified": False,
        "return_to_savings_attempted": False,
        "executor_must_repeat_fresh_preflight": True,
        "atomic_compare_and_swap_guaranteed": False,
        "preserved_fields": list(PRESERVED_FIELDS),
        "grid_charging_state_known": False,
        "grid_charging_actual_state_verified": False,
        "executor_must_send_mode_field_only": True,
    }

    def block(code: str, status: str = "blocked") -> dict[str, Any]:
        audit["reason_codes"].append(code)
        audit["status"] = status
        return audit

    if clock is None:
        return block("assessment_clock_invalid")
    if not isinstance(state, dict):
        state = {}
    try:
        decision = evaluate_guard(state, now=clock)
    except Exception:
        return block("guard_assessment_failed")
    audit["guard_action"] = decision["proposed_action"]
    audit["guard_reason_codes"] = list(decision["reason_codes"])
    if state.get("mode") == "self_consumption":
        return block("snapshot_already_self_powered", "already_self_powered")
    if decision["proposed_action"] != "propose_self_powered_fallback":
        return block("guard_does_not_propose_fallback")
    if (state.get("publisher_owns_installed_tariff") is not True
        or not decision["mode_change_permitted_by_policy"]):
        return block("publisher_ownership_or_policy_unverified")
    lease_until = _instant(state.get("guard_lease_until"))
    if state.get("guard_lease_trusted") is not True or lease_until is None:
        return block("trusted_guard_lease_required")
    if lease_until > clock:
        return block("publisher_guard_lease_not_expired")
    preserve = _preserve(state.get("preserve_settings"))
    if preserve is None:
        return block("preserved_settings_unknown")
    audit["grid_charging_state_known"] = type(preserve["grid_charging"]) is bool
    audit["eligible_for_fallback"] = True
    if live_execution_enabled is not True:
        return audit
    if executor is None:
        return block("executor_not_supplied")

    audit["executor_called"] = True
    try:
        preflight = executor.read_snapshot()
    except Exception:
        return block("preflight_read_failed")
    preflight_at = _snapshot_time(preflight, start=clock, previous=clock)
    if preflight_at is None:
        return block("preflight_observation_time_invalid")
    preflight_state = _merge_observations(state, preflight)
    if preflight_state.get("mode") == "self_consumption":
        return block("preflight_already_self_powered", "already_self_powered")
    try:
        checked = evaluate_guard(preflight_state, now=preflight_at)
    except Exception:
        return block("preflight_assessment_failed")
    if (checked["proposed_action"] != "propose_self_powered_fallback"
        or checked["mode_change_permitted_by_policy"] is not True):
        return block("preflight_controller_or_policy_changed", "preflight_aborted")
    preflight_preserve = _preserve(preflight.get("preserve_settings"))
    audit["grid_charging_state_known"] = bool(audit["grid_charging_state_known"]
        and preflight_preserve is not None and type(preflight_preserve["grid_charging"]) is bool)
    if preflight_preserve != preserve:
        return block("preflight_preserved_settings_changed", "preflight_aborted")
    expected_hash = state["last_verified_tariff_hash"]
    audit["preflight_verified"] = True
    audit["command_attempted"] = True
    command_result = None
    try:
        # Executor contract requires another fresh preflight immediately before
        # its POST, including concurrent publisher-lease renewal detection.
        command_result = executor.set_self_powered(
            expected_tariff_hash=expected_hash, preserve_settings=dict(preserve))
    except Exception:
        # A timeout may occur after a request reached Tesla. Do not claim zero
        # effects and do not expose exception messages containing auth details.
        audit["command_sent"] = None
        audit["action_executed"] = None
        audit["control_requests"] = None
        audit["reason_codes"].append("command_outcome_unknown")
    if command_result is not None:
        if not isinstance(command_result, dict) or type(command_result.get("command_sent")) is not bool:
            audit["command_sent"] = None
            audit["action_executed"] = None
            audit["control_requests"] = None
            audit["reason_codes"].append("command_result_invalid")
        elif command_result["command_sent"] is False:
            return block("executor_preflight_or_policy_aborted", "preflight_aborted")
        else:
            audit["command_sent"] = True
            audit["action_executed"] = True
            audit["control_requests"] = 1
            audit["executor_preflight_attested"] = command_result.get("preflight_verified") is True
            if not audit["executor_preflight_attested"]:
                audit["reason_codes"].append("executor_preflight_not_attested")
    elif not audit["reason_codes"]:
        audit["command_sent"] = None
        audit["action_executed"] = None
        audit["control_requests"] = None
        audit["reason_codes"].append("command_result_invalid")

    try:
        postflight = executor.read_snapshot()
    except Exception:
        return block("postflight_read_failed", "fallback_unverified")
    postflight_at = _snapshot_time(postflight, start=clock, previous=preflight_at)
    if postflight_at is None:
        return block("postflight_observation_time_invalid", "fallback_unverified")
    post_state = _merge_observations(state, postflight)
    try:
        post_checked = evaluate_guard(post_state, now=postflight_at)
    except Exception:
        return block("postflight_assessment_failed", "fallback_unverified")
    post_reasons = post_checked["reason_codes"]
    fresh_live = not any(code.startswith("live_last_success_") for code in post_reasons)
    post_preserve = _preserve(postflight.get("preserve_settings"))
    audit["grid_charging_state_known"] = bool(audit["grid_charging_state_known"]
        and post_preserve is not None and type(post_preserve["grid_charging"]) is bool)
    audit["grid_charging_actual_state_verified"] = bool(audit["grid_charging_state_known"]
        and fresh_live and post_preserve["grid_charging"] == preserve["grid_charging"])
    proofs = {
        "mode_self_powered": post_state.get("mode") == "self_consumption",
        "tariff_unchanged": post_state.get("current_tariff_hash") == expected_hash,
        "reported_preserved_settings_unchanged": post_preserve == preserve,
        "fresh_live_readback": fresh_live,
        "optimise_still_confirmed_unenrolled": post_checked["confirmed_optimise_unenrolled"],
    }
    audit["postflight_proofs"] = proofs
    state_verified = all(proofs.values())
    audit["desired_state_verified"] = state_verified
    audit["fallback_verified"] = bool(state_verified and audit["command_sent"] is True
        and audit["executor_preflight_attested"] and not audit["reason_codes"])
    if not state_verified:
        return block("postflight_state_not_verified", "fallback_unverified")
    if not audit["fallback_verified"]:
        audit["status"] = "fallback_state_verified_command_unconfirmed"
    else:
        audit["status"] = "fallback_verified"
    return audit
