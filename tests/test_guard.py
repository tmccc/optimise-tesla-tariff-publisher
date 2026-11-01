"""Policy tests only; no Tesla, HA, network, or control actions."""
import copy
from datetime import datetime, timedelta, timezone
import unittest

from guard import evaluate_guard

UTC = timezone.utc
NOW = datetime(2026, 11, 1, 12, 30, tzinfo=UTC)


def good_state():
    return {
        "source_last_success": NOW - timedelta(minutes=2),
        "source_captured_at": NOW - timedelta(minutes=1),
        "known_valid_from": NOW - timedelta(minutes=30),
        "known_valid_until": NOW + timedelta(hours=23, minutes=30),
        "live_last_success": NOW - timedelta(seconds=10),
        "battery_enrolled": False,
        "enrolled_last_success": NOW - timedelta(minutes=5),
        "energy_commands_granted": True,
        "mode": "autonomous",
        "current_tariff_hash": "a" * 64,
        "last_verified_tariff_hash": "a" * 64,
        "intended_tariff_hash": "b" * 64,
        "guard_lease_until": NOW + timedelta(minutes=3),
        "watchdog_verified": True,
        "fallback_policy": "self_powered",
        "fallback_approved": True,
        "publisher_owns_installed_tariff": True,
    }


class GuardTests(unittest.TestCase):
    def assess(self, **changes):
        state = good_state()
        state.update(changes)
        return evaluate_guard(state, now=NOW)

    def test_fresh_state_can_publish_different_next_tariff(self):
        result = self.assess()
        self.assertTrue(result["allow_publish"])
        self.assertEqual(result["reason_codes"], [])
        self.assertFalse(result["action_executed"])

    def test_does_not_mutate_input(self):
        state = good_state()
        before = copy.deepcopy(state)
        evaluate_guard(state, now=NOW)
        self.assertEqual(state, before)

    def test_missing_fields_fail_closed(self):
        result = evaluate_guard({}, now=NOW)
        self.assertFalse(result["allow_publish"])
        self.assertFalse(result["mode_change_permitted_by_policy"])

    def test_non_dict_fails_closed(self):
        self.assertFalse(evaluate_guard(None, now=NOW)["allow_publish"])

    def test_naive_assessment_clock_rejected(self):
        with self.assertRaises(ValueError):
            evaluate_guard(good_state(), now=NOW.replace(tzinfo=None))

    def test_stale_price_boundary_requests_proposed_fallback(self):
        result = self.assess(source_last_success=NOW-timedelta(minutes=15))
        self.assertFalse(result["allow_publish"])
        self.assertIn("source_last_success_stale", result["reason_codes"])
        self.assertEqual(result["proposed_action"], "propose_self_powered_fallback")
        self.assertFalse(result["action_executed"])

    def test_stale_capture_blocks_even_when_success_fresh(self):
        result = self.assess(source_captured_at=NOW-timedelta(minutes=15))
        self.assertIn("source_captured_at_stale", result["reason_codes"])

    def test_future_source_blocked(self):
        for field in ("source_last_success", "source_captured_at", "live_last_success", "enrolled_last_success"):
            with self.subTest(field=field):
                result = self.assess(**{field: NOW+timedelta(seconds=1)})
                self.assertFalse(result["allow_publish"])
                self.assertIn(field+"_in_future", result["reason_codes"])

    def test_source_cannot_succeed_after_it_was_captured(self):
        result = self.assess(source_last_success=NOW-timedelta(seconds=30))
        self.assertIn("source_success_after_capture", result["reason_codes"])

    def test_quotes_expire_at_exact_boundary(self):
        result = self.assess(known_valid_until=NOW)
        self.assertIn("quoted_window_expired", result["reason_codes"])
        self.assertEqual(result["proposed_action"], "propose_self_powered_fallback")

    def test_quote_window_cannot_start_in_future(self):
        result = self.assess(known_valid_from=NOW+timedelta(minutes=1))
        self.assertIn("quoted_window_not_started", result["reason_codes"])

    def test_quote_window_cannot_be_reversed(self):
        result = self.assess(known_valid_from=NOW+timedelta(days=2))
        self.assertIn("quoted_window_invalid", result["reason_codes"])

    def test_activation_never_proposes_override_of_optimise(self):
        result = self.assess(battery_enrolled=True, known_valid_until=NOW)
        self.assertEqual(result["proposed_action"], "handover_to_optimise_without_control_writes")
        self.assertFalse(result["allow_publish"])
        self.assertFalse(result["mode_change_permitted_by_policy"])

    def test_old_positive_enrollment_still_prevents_fallback(self):
        result = self.assess(battery_enrolled=True, enrolled_last_success=NOW-timedelta(hours=1))
        self.assertEqual(result["proposed_action"], "handover_to_optimise_without_control_writes")
        self.assertFalse(result["mode_change_permitted_by_policy"])

    def test_unknown_enrollment_never_assumes_no_controller(self):
        for enrollment in (None, 0, 1, "false", "unavailable"):
            with self.subTest(enrollment=enrollment):
                result = self.assess(battery_enrolled=enrollment)
                self.assertFalse(result["confirmed_optimise_unenrolled"])
                self.assertFalse(result["mode_change_permitted_by_policy"])
                self.assertEqual(result["proposed_action"], "pause_publishing_and_verify_controller")

    def test_enrollment_age_boundary_blocks_mode_change(self):
        result = self.assess(enrolled_last_success=NOW-timedelta(minutes=45))
        self.assertIn("enrolled_last_success_stale", result["reason_codes"])
        self.assertFalse(result["mode_change_permitted_by_policy"])
        self.assertEqual(result["proposed_action"], "pause_publishing_and_verify_controller")

    def test_lost_live_readback_blocks(self):
        result = self.assess(live_last_success=NOW-timedelta(minutes=5))
        self.assertIn("live_last_success_stale", result["reason_codes"])
        self.assertEqual(result["proposed_action"], "pause_publishing_and_verify_controller")
        self.assertFalse(result["mode_change_permitted_by_policy"])

    def test_drift_blocks(self):
        result = self.assess(current_tariff_hash="c"*64)
        self.assertIn("installed_tariff_drift", result["reason_codes"])
        self.assertEqual(result["proposed_action"], "pause_publishing_and_verify_controller")
        self.assertFalse(result["mode_change_permitted_by_policy"])

    def test_drift_with_expired_prices_never_overrides_external_controller(self):
        result = self.assess(current_tariff_hash="c"*64, known_valid_until=NOW)
        self.assertEqual(result["proposed_action"], "pause_publishing_and_verify_controller")
        self.assertFalse(result["mode_change_permitted_by_policy"])

    def test_missing_or_malformed_hashes_block(self):
        for field in ("current_tariff_hash", "last_verified_tariff_hash", "intended_tariff_hash"):
            with self.subTest(field=field):
                self.assertFalse(self.assess(**{field:"not-a-sha256"})["allow_publish"])

    def test_guard_lease_expires_at_boundary(self):
        result = self.assess(guard_lease_until=NOW)
        self.assertIn("guard_lease_expired", result["reason_codes"])
        self.assertEqual(result["proposed_action"], "propose_self_powered_fallback")

    def test_lease_cannot_outlive_quotes(self):
        result = self.assess(known_valid_until=NOW+timedelta(minutes=2))
        self.assertIn("guard_lease_exceeds_safe_deadline", result["reason_codes"])

    def test_lease_cannot_outlive_source_freshness(self):
        result = self.assess(guard_lease_until=NOW+timedelta(minutes=14))
        self.assertIn("guard_lease_exceeds_safe_deadline", result["reason_codes"])

    def test_lease_at_safe_deadline_is_allowed(self):
        result = self.assess(guard_lease_until=NOW+timedelta(minutes=4, seconds=50))
        self.assertTrue(result["allow_publish"])

    def test_lease_cannot_outlive_fresh_live_readback(self):
        result = self.assess(guard_lease_until=NOW+timedelta(minutes=5))
        self.assertIn("guard_lease_exceeds_safe_deadline", result["reason_codes"])

    def test_missing_independent_watchdog_blocks_publication(self):
        result = self.assess(watchdog_verified=False)
        self.assertFalse(result["allow_publish"])
        self.assertIn("independent_watchdog_unverified", result["reason_codes"])
        self.assertFalse(result["stopping_uploads_expires_tesla_tariff"])

    def test_unapproved_fallback_is_not_automatically_proposed_as_executable(self):
        result = self.assess(fallback_approved=False, guard_lease_until=NOW)
        self.assertEqual(result["proposed_action"], "request_operator_fallback")
        self.assertFalse(result["mode_change_permitted_by_policy"])

    def test_missing_command_permission_requires_operator(self):
        result = self.assess(energy_commands_granted=False)
        self.assertEqual(result["proposed_action"], "request_operator_fallback")
        self.assertFalse(result["mode_change_permitted_by_policy"])

    def test_self_powered_mode_does_not_request_unnecessary_mode_change(self):
        result = self.assess(mode="self_consumption", source_last_success=NOW-timedelta(hours=1))
        self.assertEqual(result["proposed_action"], "complete_safety_prerequisites")

    def test_unknown_mode_does_not_publish(self):
        result = self.assess(mode="unknown")
        self.assertFalse(result["allow_publish"])
        self.assertEqual(result["proposed_action"], "pause_publishing_and_verify_controller")
        self.assertFalse(result["mode_change_permitted_by_policy"])

    def test_malformed_mode_fails_closed(self):
        self.assertFalse(self.assess(mode=[])["allow_publish"])

    def test_fresh_state_does_not_propose_unsolicited_mode_change(self):
        result = self.assess()
        self.assertFalse(result["mode_change_permitted_by_policy"])

    def test_initial_publish_does_not_require_ownership_of_existing_baseline(self):
        result = self.assess(publisher_owns_installed_tariff=False)
        self.assertTrue(result["allow_publish"])
        self.assertFalse(result["mode_change_permitted_by_policy"])
        self.assertNotIn("publisher_ownership_unverified", result["reason_codes"])

    def test_expired_unowned_existing_savings_never_changes_mode(self):
        result = self.assess(publisher_owns_installed_tariff=False, known_valid_until=NOW)
        self.assertIn("publisher_ownership_unverified", result["reason_codes"])
        self.assertEqual(result["proposed_action"], "request_operator_fallback")
        self.assertFalse(result["mode_change_permitted_by_policy"])

    def test_missing_ownership_is_not_inferred_from_equal_tariff_hashes(self):
        state = good_state()
        state.pop("publisher_owns_installed_tariff")
        state["known_valid_until"] = NOW
        self.assertEqual(state["current_tariff_hash"], state["last_verified_tariff_hash"])
        result = evaluate_guard(state, now=NOW)
        self.assertIn("publisher_ownership_unverified", result["reason_codes"])
        self.assertFalse(result["publisher_owns_installed_tariff"])
        self.assertFalse(result["mode_change_permitted_by_policy"])

    def test_equivalent_timezone_instants_work(self):
        result = self.assess(source_captured_at="2026-11-01T13:29:00+01:00")
        self.assertTrue(result["allow_publish"])

    def test_naive_input_timestamp_fails_closed(self):
        result = self.assess(source_captured_at="2026-11-01T12:29:00")
        self.assertIn("source_captured_at_unknown", result["reason_codes"])


if __name__ == "__main__":
    unittest.main()
