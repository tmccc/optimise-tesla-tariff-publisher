"""Fallback orchestration with injected fakes; no network or real commands."""
import copy
from datetime import datetime, timedelta, timezone
import json
import unittest

from watchdog import evaluate_cycle

UTC = timezone.utc
NOW = datetime(2026, 11, 1, 12, 30, tzinfo=UTC)


def fallback_state():
    return {
        "source_last_success": NOW - timedelta(minutes=16),
        "source_captured_at": NOW - timedelta(minutes=1),
        "known_valid_from": NOW - timedelta(minutes=30),
        "known_valid_until": NOW + timedelta(hours=23),
        "live_last_success": NOW - timedelta(seconds=10),
        "battery_enrolled": False,
        "enrolled_last_success": NOW - timedelta(minutes=5),
        "energy_commands_granted": True,
        "mode": "autonomous",
        "current_tariff_hash": "a" * 64,
        "last_verified_tariff_hash": "a" * 64,
        "intended_tariff_hash": "b" * 64,
        "guard_lease_until": NOW - timedelta(seconds=1),
        "guard_lease_trusted": True,
        "watchdog_verified": True,
        "fallback_policy": "self_powered",
        "fallback_approved": True,
        "publisher_owns_installed_tariff": True,
        "preserve_settings": {
            "backup_reserve": 10,
            "grid_charging": True,
            "export_permission": "battery_ok",
        },
    }


def snapshot(*, second=1, mode="autonomous", **changes):
    observed = NOW + timedelta(seconds=second)
    value = {
        "observed_at": observed,
        "live_last_success": observed,
        "battery_enrolled": False,
        "enrolled_last_success": observed,
        "energy_commands_granted": True,
        "mode": mode,
        "current_tariff_hash": "a" * 64,
        "preserve_settings": dict(fallback_state()["preserve_settings"]),
    }
    value.update(changes)
    return value


class Executor:
    def __init__(self, *, preflight=None, postflight=None, result=None):
        self.snapshots = [
            snapshot() if preflight is None else preflight,
            snapshot(second=2, mode="self_consumption") if postflight is None else postflight,
        ]
        self.result = {"preflight_verified": True, "command_sent": True} if result is None else result
        self.calls = []
        self.commands = []
        self.actual_posts = 0

    def read_snapshot(self):
        self.calls.append("read_snapshot")
        value = self.snapshots.pop(0)
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def set_self_powered(self, *, expected_tariff_hash, preserve_settings):
        self.calls.append("set_self_powered")
        self.commands.append({"expected_tariff_hash": expected_tariff_hash,
                              "preserve_settings": copy.deepcopy(preserve_settings)})
        if isinstance(self.result, Exception):
            raise self.result
        if isinstance(self.result, dict) and self.result.get("command_sent") is True:
            self.actual_posts += 1
        return self.result

    def set_savings(self, **_kwargs):
        raise AssertionError("Returning to Savings is prohibited")


class WatchdogTests(unittest.TestCase):
    def run_cycle(self, *, executor=None, live=True, **changes):
        state = fallback_state()
        state.update(changes)
        return evaluate_cycle(state, now=NOW, executor=executor, live_execution_enabled=live)

    def test_default_observes_only_even_with_an_executor(self):
        executor = Executor()
        result = evaluate_cycle(fallback_state(), now=NOW, executor=executor)
        self.assertTrue(result["eligible_for_fallback"])
        self.assertEqual(result["status"], "observe_only")
        self.assertEqual(executor.calls, [])
        self.assertFalse(result["action_executed"])

    def test_no_executor_default_has_no_effects(self):
        result = evaluate_cycle(fallback_state(), now=NOW)
        self.assertFalse(result["executor_called"])
        self.assertEqual(result["control_requests"], 0)

    def test_live_flag_requires_actual_true(self):
        executor = Executor()
        self.run_cycle(executor=executor, live=1)
        self.assertEqual(executor.calls, [])

    def test_safe_owned_expired_lease_can_execute_and_verify(self):
        executor = Executor()
        result = self.run_cycle(executor=executor)
        self.assertEqual(result["status"], "fallback_verified")
        self.assertTrue(result["fallback_verified"])
        self.assertTrue(result["action_executed"])
        self.assertTrue(result["executor_preflight_attested"])
        self.assertTrue(result["grid_charging_state_known"])
        self.assertTrue(result["grid_charging_actual_state_verified"])
        self.assertEqual(result["control_requests"], 1)
        self.assertEqual(executor.calls, ["read_snapshot", "set_self_powered", "read_snapshot"])
        self.assertEqual(executor.commands[0]["expected_tariff_hash"], "a"*64)
        self.assertEqual(executor.commands[0]["preserve_settings"], fallback_state()["preserve_settings"])
        self.assertFalse(result["atomic_compare_and_swap_guaranteed"])

    def test_initial_state_and_lease_are_unchanged(self):
        state = fallback_state()
        before = copy.deepcopy(state)
        evaluate_cycle(state, now=NOW, executor=Executor(), live_execution_enabled=True)
        self.assertEqual(state, before)

    def test_live_without_executor_is_blocked(self):
        result = self.run_cycle()
        self.assertIn("executor_not_supplied", result["reason_codes"])
        self.assertFalse(result["action_executed"])

    def test_unowned_baseline_is_not_acted_on(self):
        executor = Executor()
        result = self.run_cycle(executor=executor, publisher_owns_installed_tariff=False)
        self.assertFalse(result["eligible_for_fallback"])
        self.assertEqual(executor.calls, [])

    def test_trusted_lease_must_be_actual_true(self):
        for trusted in (None, False, 1, "true"):
            with self.subTest(trusted=trusted):
                executor = Executor()
                result = self.run_cycle(executor=executor, guard_lease_trusted=trusted)
                self.assertIn("trusted_guard_lease_required", result["reason_codes"])
                self.assertEqual(executor.calls, [])

    def test_missing_lease_cannot_trigger_fallback(self):
        executor = Executor()
        result = self.run_cycle(executor=executor, guard_lease_until=None)
        self.assertIn("trusted_guard_lease_required", result["reason_codes"])
        self.assertEqual(executor.calls, [])

    def test_future_lease_cannot_trigger_fallback_despite_stale_prices(self):
        executor = Executor()
        result = self.run_cycle(executor=executor, guard_lease_until=NOW+timedelta(minutes=1))
        self.assertIn("publisher_guard_lease_not_expired", result["reason_codes"])
        self.assertEqual(executor.calls, [])

    def test_lease_expiring_exactly_now_is_eligible(self):
        result = self.run_cycle(executor=Executor(), guard_lease_until=NOW)
        self.assertTrue(result["fallback_verified"])

    def test_positive_unknown_or_stale_enrollment_never_calls_executor(self):
        scenarios = [
            {"battery_enrolled": True}, {"battery_enrolled": None},
            {"battery_enrolled": "false"},
            {"enrolled_last_success": NOW-timedelta(minutes=45)},
        ]
        for changes in scenarios:
            with self.subTest(changes=changes):
                executor = Executor()
                result = self.run_cycle(executor=executor, **changes)
                self.assertFalse(result["action_executed"])
                self.assertEqual(executor.calls, [])

    def test_drift_missing_readback_or_unknown_mode_never_calls_executor(self):
        for changes in ({"current_tariff_hash": "c"*64}, {"live_last_success": None},
                        {"mode": "unknown"}, {"energy_commands_granted": False}):
            with self.subTest(changes=changes):
                executor = Executor()
                self.run_cycle(executor=executor, **changes)
                self.assertEqual(executor.calls, [])

    def test_unapproved_fallback_never_calls_executor(self):
        executor = Executor()
        self.run_cycle(executor=executor, fallback_approved=False)
        self.assertEqual(executor.calls, [])

    def test_unknown_preserved_settings_block(self):
        for key, value in (("backup_reserve", None), ("backup_reserve", True),
                           ("backup_reserve", 10**1000), ("backup_reserve", float("nan")),
                           ("grid_charging", 1), ("grid_charging", None),
                           ("export_permission", "unknown")):
            with self.subTest(key=key, value=type(value).__name__):
                preserve = dict(fallback_state()["preserve_settings"], **{key:value})
                executor = Executor()
                result = self.run_cycle(executor=executor, preserve_settings=preserve)
                self.assertIn("preserved_settings_unknown", result["reason_codes"])
                self.assertEqual(executor.calls, [])

    def test_initial_self_powered_never_posts(self):
        executor = Executor()
        result = self.run_cycle(executor=executor, mode="self_consumption")
        self.assertEqual(result["status"], "already_self_powered")
        self.assertEqual(executor.calls, [])

    def test_preflight_self_powered_never_posts(self):
        executor = Executor(preflight=snapshot(mode="self_consumption"))
        result = self.run_cycle(executor=executor)
        self.assertEqual(result["status"], "already_self_powered")
        self.assertEqual(executor.calls, ["read_snapshot"])

    def test_preflight_controller_changes_abort_before_setter(self):
        scenarios = [snapshot(current_tariff_hash="c"*64), snapshot(battery_enrolled=True),
            snapshot(battery_enrolled=None), snapshot(mode="unknown"),
            snapshot(live_last_success=NOW-timedelta(minutes=6)),
            snapshot(energy_commands_granted=False)]
        for preflight in scenarios:
            with self.subTest(preflight=preflight):
                executor = Executor(preflight=preflight)
                result = self.run_cycle(executor=executor)
                self.assertEqual(result["status"], "preflight_aborted")
                self.assertEqual(executor.calls, ["read_snapshot"])

    def test_preflight_settings_change_aborts(self):
        for key, value in (("backup_reserve", 20), ("grid_charging", False),
                           ("export_permission", "pv_only")):
            with self.subTest(key=key):
                preserve = dict(fallback_state()["preserve_settings"], **{key:value})
                executor = Executor(preflight=snapshot(preserve_settings=preserve))
                result = self.run_cycle(executor=executor)
                self.assertIn("preflight_preserved_settings_changed", result["reason_codes"])
                self.assertEqual(executor.calls, ["read_snapshot"])

    def test_executor_second_preflight_can_abort_concurrent_change(self):
        executor = Executor(result={"preflight_verified":False,"command_sent":False})
        result = self.run_cycle(executor=executor)
        self.assertEqual(result["status"], "preflight_aborted")
        self.assertEqual(executor.actual_posts, 0)
        self.assertFalse(result["action_executed"])
        self.assertEqual(executor.calls, ["read_snapshot", "set_self_powered"])

    def test_snapshot_cannot_replace_approval_ownership_or_lease(self):
        preflight = snapshot(fallback_approved=False, publisher_owns_installed_tariff=False,
            guard_lease_until=NOW+timedelta(days=1))
        result = self.run_cycle(executor=Executor(preflight=preflight))
        self.assertTrue(result["fallback_verified"])

    def test_missing_future_or_replayed_observation_time_blocks(self):
        for observed in (None, NOW-timedelta(seconds=1), NOW+timedelta(minutes=6),
                         "2026-11-01T12:30:00"):
            with self.subTest(observed=observed):
                executor = Executor(preflight=snapshot(observed_at=observed))
                result = self.run_cycle(executor=executor)
                self.assertIn("preflight_observation_time_invalid", result["reason_codes"])
                self.assertEqual(executor.calls, ["read_snapshot"])

    def test_fresh_observations_after_initial_clock_are_supported(self):
        result = self.run_cycle(executor=Executor())
        self.assertTrue(result["fallback_verified"])

    def test_postflight_preserve_failures_do_not_return_to_savings(self):
        for key, value in (("backup_reserve", 20), ("grid_charging", False),
                           ("export_permission", "pv_only")):
            with self.subTest(key=key):
                preserve = dict(fallback_state()["preserve_settings"], **{key:value})
                executor = Executor(postflight=snapshot(second=2,mode="self_consumption",preserve_settings=preserve))
                result = self.run_cycle(executor=executor)
                self.assertEqual(result["status"], "fallback_unverified")
                self.assertFalse(result["fallback_verified"])
                self.assertTrue(result["action_executed"])
                self.assertFalse(result["return_to_savings_attempted"])
                self.assertEqual(executor.actual_posts, 1)

    def test_postflight_wrong_mode_hash_or_enrollment_is_failure(self):
        scenarios = [snapshot(second=2), snapshot(second=2,mode="self_consumption",current_tariff_hash="c"*64),
            snapshot(second=2,mode="self_consumption",battery_enrolled=True),
            snapshot(second=2,mode="self_consumption",battery_enrolled=None),
            snapshot(second=2,mode="self_consumption",live_last_success=NOW-timedelta(minutes=6))]
        for postflight in scenarios:
            with self.subTest(postflight=postflight):
                result = self.run_cycle(executor=Executor(postflight=postflight))
                self.assertEqual(result["status"], "fallback_unverified")
                self.assertFalse(result["fallback_verified"])

    def test_replayed_postflight_time_is_failure(self):
        executor = Executor(postflight=snapshot(second=0,mode="self_consumption"))
        result = self.run_cycle(executor=executor)
        self.assertIn("postflight_observation_time_invalid", result["reason_codes"])
        self.assertFalse(result["fallback_verified"])

    def test_exception_contents_never_leave_audit(self):
        private = "SECRET-password-cookie-access-token"
        for where in ("preflight", "setter", "postflight"):
            with self.subTest(where=where):
                error = RuntimeError(private)
                kwargs = {"preflight":error} if where=="preflight" else (
                    {"result":error} if where=="setter" else {"postflight":error})
                result = self.run_cycle(executor=Executor(**kwargs))
                self.assertNotIn(private, json.dumps(result))
                self.assertFalse(result["fallback_verified"])
                self.assertFalse(result["return_to_savings_attempted"])
                if where=="setter":
                    self.assertIsNone(result["command_sent"])
                    self.assertIsNone(result["control_requests"])
                    self.assertTrue(result["desired_state_verified"])

    def test_missing_executor_preflight_attestation_is_not_full_success(self):
        result = self.run_cycle(executor=Executor(result={"command_sent":True}))
        self.assertTrue(result["desired_state_verified"])
        self.assertFalse(result["fallback_verified"])
        self.assertIn("executor_preflight_not_attested", result["reason_codes"])

    def test_malformed_command_result_cannot_be_claimed_successful(self):
        result = self.run_cycle(executor=Executor(result="unexpected private reply"))
        self.assertIsNone(result["command_sent"])
        self.assertFalse(result["fallback_verified"])
        self.assertNotIn("unexpected private reply", json.dumps(result))

    def test_healthy_publisher_cycle_does_not_call_executor(self):
        executor = Executor()
        result = self.run_cycle(executor=executor, source_last_success=NOW-timedelta(minutes=1),
            source_captured_at=NOW, guard_lease_until=NOW+timedelta(minutes=2))
        self.assertFalse(result["eligible_for_fallback"])
        self.assertEqual(executor.calls, [])

    def test_invalid_clock_or_input_fail_closed(self):
        executor = Executor()
        result = evaluate_cycle(fallback_state(), now=NOW.replace(tzinfo=None),
            executor=executor, live_execution_enabled=True)
        self.assertIn("assessment_clock_invalid", result["reason_codes"])
        self.assertEqual(executor.calls, [])
        self.assertFalse(evaluate_cycle(None,now=NOW)["eligible_for_fallback"])

    def test_stable_api_omission_is_opaque_not_a_grid_permission_claim(self):
        preserve = dict(fallback_state()["preserve_settings"], grid_charging="not_reported")
        executor = Executor(preflight=snapshot(preserve_settings=preserve),
            postflight=snapshot(second=2,mode="self_consumption",preserve_settings=preserve))
        result = self.run_cycle(executor=executor, preserve_settings=preserve)
        self.assertTrue(result["fallback_verified"])
        self.assertFalse(result["grid_charging_state_known"])
        self.assertFalse(result["grid_charging_actual_state_verified"])
        self.assertTrue(result["postflight_proofs"]["reported_preserved_settings_unchanged"])
        self.assertTrue(result["executor_must_send_mode_field_only"])
        self.assertEqual(executor.commands[0]["preserve_settings"]["grid_charging"], "not_reported")

    def test_missing_grid_key_is_not_automatically_treated_as_omission_sentinel(self):
        preserve = dict(fallback_state()["preserve_settings"])
        preserve.pop("grid_charging")
        executor = Executor()
        result = self.run_cycle(executor=executor,preserve_settings=preserve)
        self.assertIn("preserved_settings_unknown", result["reason_codes"])
        self.assertEqual(executor.calls, [])

    def test_preflight_grid_report_changes_to_or_from_bool_abort(self):
        for before, after in (("not_reported", True),("not_reported", False),
                              (True, "not_reported"),(False, "not_reported")):
            with self.subTest(before=before, after=after):
                initial = dict(fallback_state()["preserve_settings"],grid_charging=before)
                changed = dict(initial,grid_charging=after)
                executor = Executor(preflight=snapshot(preserve_settings=changed))
                result = self.run_cycle(executor=executor,preserve_settings=initial)
                self.assertEqual(result["status"], "preflight_aborted")
                self.assertEqual(executor.calls, ["read_snapshot"])
                self.assertFalse(result["grid_charging_state_known"])

    def test_postflight_grid_report_changes_to_or_from_bool_fail_verification(self):
        for before, after in (("not_reported", True),("not_reported", False),
                              (True, "not_reported"),(False, "not_reported")):
            with self.subTest(before=before, after=after):
                initial = dict(fallback_state()["preserve_settings"],grid_charging=before)
                changed = dict(initial,grid_charging=after)
                executor = Executor(preflight=snapshot(preserve_settings=initial),
                    postflight=snapshot(second=2,mode="self_consumption",preserve_settings=changed))
                result = self.run_cycle(executor=executor,preserve_settings=initial)
                self.assertEqual(result["status"], "fallback_unverified")
                self.assertFalse(result["fallback_verified"])
                self.assertFalse(result["grid_charging_state_known"])
                self.assertFalse(result["grid_charging_actual_state_verified"])

    def test_observe_omitted_grid_state_never_claims_it_is_known(self):
        preserve = dict(fallback_state()["preserve_settings"],grid_charging="not_reported")
        executor = Executor()
        result = self.run_cycle(executor=executor,live=False,preserve_settings=preserve)
        self.assertEqual(result["status"], "observe_only")
        self.assertFalse(result["grid_charging_state_known"])
        self.assertFalse(result["grid_charging_actual_state_verified"])
        self.assertEqual(executor.calls, [])


if __name__ == "__main__":
    unittest.main()
