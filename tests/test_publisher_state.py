"""Pure lease/ownership tests: no PIDs checked or heartbeat files fabricated."""
import copy
from datetime import datetime, timedelta, timezone
import json
import unittest

from publisher_state import plan_publish, reconcile_supervised_transition, verified_publish_records

UTC = timezone.utc
NOW = datetime(2026, 11, 1, 13, 0, tzinfo=UTC)


def good_state():
    return {
        "source_last_success": NOW-timedelta(minutes=1),
        "source_captured_at": NOW-timedelta(seconds=30),
        "known_valid_from": NOW-timedelta(minutes=30),
        "known_valid_until": NOW+timedelta(hours=23),
        "live_last_success": NOW-timedelta(seconds=5),
        "battery_enrolled": False,
        "enrolled_last_success": NOW-timedelta(minutes=5),
        "energy_commands_granted": True,
        "mode": "autonomous",
        "energy_site_id": 100001,
        "current_tariff_hash": "a"*64,
        "expected_baseline_hash": "a"*64,
        "candidate_tariff_hash": "b"*64,
        "candidate_valid": True,
        "candidate_prices_verified": True,
        "fallback_approved": True,
        "frozen_assumption_approved": True,
        "live_publication_enabled": True,
        "publisher_pid": 2001,
        "publisher_owns_installed_tariff": False,
        "watchdog": {"continuous":True,"armed":True,"last_cycle_at":NOW-timedelta(seconds=10),
                     "pid":2002,"pid_alive":True},
        "preserve_settings_before": {"backup_reserve":10,"grid_charging":"not_reported",
                                     "export_permission":"battery_ok"},
    }


def verified_state(*, owned_renewal=False):
    state = good_state()
    state.update(
        write_acknowledged=True,
        write_acknowledged_at=NOW-timedelta(seconds=2),
        fresh_readback_tariff_hash="b"*64,
        preserve_settings_after=dict(state["preserve_settings_before"]),
        readback={"energy_site_id":state["energy_site_id"],"observed_at":NOW-timedelta(seconds=1),
                  "live_last_success":NOW-timedelta(seconds=1,microseconds=500000),"mode":"autonomous",
                  "battery_enrolled":False,"enrolled_last_success":NOW-timedelta(minutes=1),
                  "energy_commands_granted":True,"current_tariff_hash":"b"*64,
                  "preserve_settings":dict(state["preserve_settings_before"])},
    )
    if owned_renewal:
        state.update(current_tariff_hash="b"*64,expected_baseline_hash="b"*64,
            publisher_owns_installed_tariff=True,write_acknowledged=False,
            ownership={"verified_publisher_write":True,"energy_site_id":state["energy_site_id"],
                       "tariff_semantic_hash":"b"*64,"written_at":NOW-timedelta(hours=1),
                       "last_verified_at":NOW-timedelta(minutes=10)})
    return state


class PublisherPlanTests(unittest.TestCase):
    def plan(self, **changes):
        state = good_state()
        state.update(changes)
        return plan_publish(state,now=NOW)

    def test_complete_checks_allow_first_write_but_never_create_ownership(self):
        result = self.plan()
        self.assertTrue(result["allow_write"])
        self.assertTrue(result["write_required"])
        self.assertFalse(result["ownership_created"])
        self.assertEqual(result["control_requests"],0)
        self.assertEqual(datetime.fromisoformat(result["proposed_guard_lease_until"]),NOW+timedelta(minutes=3))

    def test_all_flags_require_real_true(self):
        for key in ("candidate_valid","candidate_prices_verified","fallback_approved",
                    "frozen_assumption_approved","live_publication_enabled","energy_commands_granted"):
            for value in (False,None,1,"true"):
                with self.subTest(key=key,value=value):
                    self.assertFalse(self.plan(**{key:value})["allow_write"])

    def test_source_live_and_enrollment_freshness_boundaries(self):
        for key,age in (("source_last_success",timedelta(minutes=15)),
                       ("source_captured_at",timedelta(minutes=15)),
                       ("live_last_success",timedelta(minutes=5)),
                       ("enrolled_last_success",timedelta(minutes=45))):
            with self.subTest(key=key):
                self.assertFalse(self.plan(**{key:NOW-age})["allow_write"])

    def test_future_timestamps_are_not_fresh(self):
        for key in ("source_last_success","source_captured_at","live_last_success","enrolled_last_success"):
            with self.subTest(key=key):
                self.assertFalse(self.plan(**{key:NOW+timedelta(seconds=1)})["allow_write"])

    def test_source_success_cannot_postdate_snapshot(self):
        result = self.plan(source_last_success=NOW-timedelta(seconds=1))
        self.assertIn("source_success_after_capture",result["reason_codes"])

    def test_quoted_window_must_include_current_instant(self):
        for changes in ({"known_valid_from":NOW+timedelta(seconds=1)},
                        {"known_valid_until":NOW},{"known_valid_until":None},
                        {"known_valid_from":NOW+timedelta(days=2)}):
            with self.subTest(changes=changes):
                self.assertFalse(self.plan(**changes)["allow_write"])

    def test_exact_known_false_enrollment_and_savings_are_required(self):
        for enrolled in (True,None,0,"false","unavailable"):
            with self.subTest(enrolled=enrolled):
                self.assertFalse(self.plan(battery_enrolled=enrolled)["allow_write"])
        for mode in ("self_consumption",None,[],"unknown"):
            with self.subTest(mode=mode):
                self.assertFalse(self.plan(mode=mode)["allow_write"])

    def test_drift_and_malformed_hashes_block(self):
        self.assertIn("installed_tariff_changed",self.plan(current_tariff_hash="c"*64)["reason_codes"])
        for key in ("current_tariff_hash","expected_baseline_hash","candidate_tariff_hash"):
            with self.subTest(key=key):
                self.assertFalse(self.plan(**{key:"not-a-sha256"})["allow_write"])

    def test_known_site_and_process_identities_required(self):
        for key in ("energy_site_id","publisher_pid"):
            for value in (None,False,True,0,-1,"2001"):
                with self.subTest(key=key,value=value):
                    self.assertFalse(self.plan(**{key:value})["allow_write"])

    def test_watchdog_must_be_running_continuous_armed_and_separate(self):
        for key,value in (("continuous",False),("armed",False),("pid_alive",False),
                          ("pid_alive",1),("pid",2001),("pid",None)):
            with self.subTest(key=key,value=value):
                watchdog = dict(good_state()["watchdog"],**{key:value})
                self.assertFalse(self.plan(watchdog=watchdog)["allow_write"])

    def test_watchdog_heartbeat_age_boundary_and_future_clock(self):
        for stamp in (NOW-timedelta(seconds=90),NOW+timedelta(seconds=1),None):
            with self.subTest(stamp=stamp):
                self.assertFalse(self.plan(watchdog=dict(good_state()["watchdog"],last_cycle_at=stamp))["allow_write"])
        self.assertTrue(self.plan(watchdog=dict(good_state()["watchdog"],last_cycle_at=NOW-timedelta(seconds=89)))["allow_write"])

    def test_missing_watchdog_is_not_fabricated(self):
        state = good_state()
        state.pop("watchdog")
        result = plan_publish(state,now=NOW)
        self.assertFalse(result["allow_write"])
        self.assertNotIn("watchdog",state)

    def test_deadline_is_clipped_to_each_safety_window(self):
        scenarios = [
            ({"known_valid_until":NOW+timedelta(seconds=45)},45),
            ({"source_last_success":NOW-timedelta(minutes=14,seconds=15)},45),
            ({"live_last_success":NOW-timedelta(minutes=4,seconds=15)},45),
            ({"enrolled_last_success":NOW-timedelta(minutes=44,seconds=15)},45),
        ]
        for changes,seconds in scenarios:
            with self.subTest(changes=changes):
                result = self.plan(**changes)
                self.assertTrue(result["allow_write"])
                self.assertEqual(datetime.fromisoformat(result["proposed_guard_lease_until"]),NOW+timedelta(seconds=seconds))

    def test_too_little_headroom_blocks_starting_a_write(self):
        result = self.plan(known_valid_until=NOW+timedelta(seconds=29))
        self.assertIn("insufficient_lease_headroom",result["reason_codes"])
        self.assertFalse(result["allow_write"])
        self.assertTrue(self.plan(known_valid_until=NOW+timedelta(seconds=30))["allow_write"])

    def test_same_hash_without_ownership_still_needs_an_acknowledged_write(self):
        result = self.plan(candidate_tariff_hash="a"*64)
        self.assertTrue(result["allow_write"])
        self.assertTrue(result["write_required"])
        self.assertFalse(result["allow_renewal_without_post"])

    def test_same_hash_with_verified_ownership_can_renew_without_post(self):
        result = plan_publish(verified_state(owned_renewal=True),now=NOW)
        self.assertTrue(result["allow_write"])
        self.assertFalse(result["write_required"])
        self.assertTrue(result["allow_renewal_without_post"])

    def test_matching_hashes_do_not_replace_verified_ownership_record(self):
        for change in ({"ownership":None},{"publisher_owns_installed_tariff":False},
                       {"ownership":{"verified_publisher_write":True}}):
            with self.subTest(change=change):
                state = verified_state(owned_renewal=True)
                state.update(change)
                self.assertTrue(plan_publish(state,now=NOW)["write_required"])

    def test_bad_ownership_identity_or_times_cannot_authorise_renewal(self):
        for key,value in (("verified_publisher_write",1),("energy_site_id",9),
                          ("tariff_semantic_hash","c"*64),("written_at",NOW+timedelta(seconds=1)),
                          ("last_verified_at",None)):
            with self.subTest(key=key):
                state = verified_state(owned_renewal=True)
                state["ownership"][key] = value
                self.assertTrue(plan_publish(state,now=NOW)["write_required"])

    def test_protected_values_are_required_without_guessing_missing_grid(self):
        for field,value in (("backup_reserve",None),("backup_reserve",True),
                            ("grid_charging",None),("grid_charging",1),
                            ("export_permission","unknown")):
            with self.subTest(field=field,value=value):
                preserve = dict(good_state()["preserve_settings_before"],**{field:value})
                self.assertFalse(self.plan(preserve_settings_before=preserve)["allow_write"])

    def test_input_is_unchanged_and_naive_clock_fails_closed(self):
        state = good_state()
        before = copy.deepcopy(state)
        plan_publish(state,now=NOW)
        self.assertEqual(state,before)
        self.assertFalse(plan_publish(state,now=NOW.replace(tzinfo=None))["allow_write"])
        self.assertFalse(plan_publish(None,now=NOW)["allow_write"])


class VerifiedRecordTests(unittest.TestCase):
    def verify(self, state=None):
        return verified_publish_records(verified_state() if state is None else state,verified_at=NOW)

    def test_acknowledged_matching_postflight_produces_ownership_and_bounded_lease(self):
        result = self.verify()
        self.assertTrue(result["verified"])
        ownership,lease = result["ownership"],result["lease"]
        self.assertIs(ownership["verified_publisher_write"],True)
        self.assertEqual(ownership["energy_site_id"],100001)
        self.assertEqual(ownership["tariff_semantic_hash"],"b"*64)
        self.assertTrue(lease["guard_lease_trusted"])
        self.assertTrue(lease["publisher_owns_installed_tariff"])
        self.assertEqual(lease["last_verified_tariff_hash"],"b"*64)
        self.assertEqual(datetime.fromisoformat(lease["guard_lease_until"]),NOW+timedelta(minutes=3))
        self.assertFalse(result["grid_charging_state_known"])
        self.assertEqual(result["control_requests"],0)

    def test_acknowledgment_is_required_for_unowned_tariff(self):
        for acknowledged in (False,None,1,"true"):
            with self.subTest(acknowledged=acknowledged):
                state = verified_state()
                state["write_acknowledged"] = acknowledged
                result = self.verify(state)
                self.assertFalse(result["verified"])
                self.assertIsNone(result["ownership"])
                self.assertIsNone(result["lease"])

    def test_same_hash_does_not_allow_adopting_an_unowned_tariff(self):
        state = verified_state()
        state.update(current_tariff_hash="b"*64,expected_baseline_hash="b"*64,write_acknowledged=False)
        self.assertFalse(self.verify(state)["verified"])

    def test_owned_same_hash_renewal_requires_no_post_and_retains_original_write_time(self):
        state = verified_state(owned_renewal=True)
        result = self.verify(state)
        self.assertTrue(result["verified"])
        self.assertEqual(result["publication_kind"],"verified_owned_lease_renewal")
        self.assertEqual(datetime.fromisoformat(result["ownership"]["written_at"]),state["ownership"]["written_at"])
        self.assertEqual(result["lease"]["issued_at"],NOW.isoformat())

    def test_owned_renewal_cannot_claim_a_different_next_tariff(self):
        state = verified_state(owned_renewal=True)
        state["candidate_tariff_hash"] = "c"*64
        state["fresh_readback_tariff_hash"] = "c"*64
        state["readback"]["current_tariff_hash"] = "c"*64
        self.assertFalse(self.verify(state)["verified"])

    def test_acknowledged_write_requires_actual_matching_readback(self):
        for value in (None,"a"*64,"invalid"):
            with self.subTest(value=value):
                state = verified_state()
                state["fresh_readback_tariff_hash"] = value
                self.assertFalse(self.verify(state)["verified"])

    def test_post_write_ordering_must_be_verified(self):
        for value in (None,NOW+timedelta(seconds=1),NOW):
            with self.subTest(value=value):
                state = verified_state()
                state["write_acknowledged_at"] = value
                result = self.verify(state)
                self.assertIn("post_write_readback_order_unverified",result["reason_codes"])
                self.assertFalse(result["verified"])

    def test_replayed_old_write_acknowledgment_is_not_a_new_verified_write(self):
        state = verified_state()
        state["write_acknowledged_at"] = NOW-timedelta(minutes=6)
        result = self.verify(state)
        self.assertIn("post_write_readback_order_unverified",result["reason_codes"])
        self.assertIsNone(result["ownership"])

    def test_postflight_mode_snapshot_cannot_predate_write_ack(self):
        state = verified_state()
        state["readback"]["live_last_success"] = NOW-timedelta(seconds=3)
        result = self.verify(state)
        self.assertIn("post_write_readback_order_unverified",result["reason_codes"])
        self.assertIsNone(result["ownership"])

    def test_changed_mode_or_enrollment_refuses_ownership(self):
        for key,value in (("mode","self_consumption"),("battery_enrolled",True),
                          ("battery_enrolled",None),("battery_enrolled",0),
                          ("energy_commands_granted",False)):
            with self.subTest(key=key,value=value):
                state = verified_state()
                state["readback"][key] = value
                result = self.verify(state)
                self.assertFalse(result["verified"])
                self.assertIsNone(result["ownership"])

    def test_readback_site_and_parallel_fields_must_agree(self):
        for key,value in (("energy_site_id",9),("current_tariff_hash","a"*64),
                          ("preserve_settings",None)):
            with self.subTest(key=key):
                state = verified_state()
                state["readback"][key] = value
                self.assertFalse(self.verify(state)["verified"])

    def test_readback_freshness_boundaries_and_future_times(self):
        scenarios = [("observed_at",NOW-timedelta(minutes=5)),
            ("live_last_success",NOW-timedelta(minutes=5)),
            ("enrolled_last_success",NOW-timedelta(minutes=45)),
            ("observed_at",NOW+timedelta(seconds=1)),
            ("live_last_success",NOW+timedelta(seconds=1)),
            ("enrolled_last_success",NOW+timedelta(seconds=1))]
        for key,value in scenarios:
            with self.subTest(key=key,value=value):
                state = verified_state()
                state["readback"][key] = value
                self.assertFalse(self.verify(state)["verified"])

    def test_reported_settings_must_remain_equal(self):
        for key,value in (("backup_reserve",20),("grid_charging",True),
                          ("export_permission","pv_only")):
            with self.subTest(key=key):
                state = verified_state()
                state["preserve_settings_after"][key] = value
                state["readback"]["preserve_settings"][key] = value
                self.assertFalse(self.verify(state)["verified"])

    def test_stable_known_grid_flag_is_marked_known(self):
        state = verified_state()
        for values in (state["preserve_settings_before"],state["preserve_settings_after"],
                       state["readback"]["preserve_settings"]):
            values["grid_charging"] = False
        self.assertTrue(self.verify(state)["grid_charging_state_known"])

    def test_verification_rechecks_source_and_watchdog_before_records(self):
        for key,value in (("source_last_success",NOW-timedelta(minutes=15)),
                          ("live_publication_enabled",False)):
            with self.subTest(key=key):
                state = verified_state()
                state[key] = value
                self.assertFalse(self.verify(state)["verified"])
        state = verified_state()
        state["watchdog"]["last_cycle_at"] = NOW-timedelta(seconds=90)
        self.assertFalse(self.verify(state)["verified"])

    def test_records_do_not_copy_arbitrary_account_or_auth_metadata(self):
        state = verified_state()
        private = "SECRET-password-token"
        state["source_metadata"] = {"access_token":private}
        state["readback"]["access_token"] = private
        result = self.verify(state)
        self.assertTrue(result["verified"])
        self.assertNotIn(private,json.dumps(result))

    def test_input_is_unchanged_and_invalid_clock_cannot_produce_records(self):
        state = verified_state()
        before = copy.deepcopy(state)
        self.verify(state)
        self.assertEqual(state,before)
        result = verified_publish_records(state,verified_at=NOW.replace(tzinfo=None))
        self.assertFalse(result["verified"])
        self.assertIsNone(result["ownership"])


def reconciliation_inputs(*, expired=False):
    state = verified_state()
    state.update(supervised_live_approval=True,live_approval_id="supervised-test-20261101",
        independent_readback_verified=True,write_acknowledged=False)
    created = NOW-timedelta(minutes=4) if expired else NOW-timedelta(seconds=10)
    first = created+timedelta(seconds=5)
    state["source_last_success"] = created-timedelta(seconds=20)
    state["source_captured_at"] = created-timedelta(seconds=10)
    preserve = dict(state["preserve_settings_before"])
    receipt = {"created_at":created,"http_status":200,"command_sent":True,
        "write_accepted":None,"code":None,"preflight_tariff_hash":"a"*64,
        "candidate_tariff_hash":"b"*64,"first_readback_tariff_hash":"b"*64,
        "first_readback_at":first,"energy_site_id":state["energy_site_id"],
        "live_approval_id":state["live_approval_id"],
        "preflight_preserve_settings":dict(preserve),
        "first_readback_preserve_settings":dict(preserve)}
    return state,receipt


class ReconciliationTests(unittest.TestCase):
    def reconcile(self, state=None, receipt=None, *, expired=False):
        default_state,default_receipt = reconciliation_inputs(expired=expired)
        return reconcile_supervised_transition(default_state if state is None else state,
            default_receipt if receipt is None else receipt,verified_at=NOW)

    def test_distinct_supervised_transition_and_two_fresh_matches_can_certify_unknown_ack(self):
        result = self.reconcile()
        self.assertTrue(result["verified"])
        self.assertFalse(result["write_acknowledged"])
        self.assertFalse(result["application_acknowledgment_known"])
        self.assertEqual(result["ownership"]["publication_kind"],"supervised_verified_http200_transition")
        self.assertTrue(result["ownership"]["written_at_is_upper_bound"])
        self.assertTrue(result["ownership"]["verified_publisher_write"])
        self.assertFalse(result["pricing_post_repeated"])
        self.assertEqual(result["control_requests"],0)

    def test_original_dispatch_deadline_is_never_extended_from_verification_time(self):
        state,receipt = reconciliation_inputs()
        result = self.reconcile(state,receipt)
        deadline = datetime.fromisoformat(result["lease"]["guard_lease_until"])
        self.assertEqual(deadline,receipt["created_at"]+timedelta(minutes=3))
        self.assertLess(deadline,NOW+timedelta(minutes=3))

    def test_expired_original_deadline_yields_only_expired_recovery_lease(self):
        state,receipt = reconciliation_inputs(expired=True)
        result = self.reconcile(state,receipt)
        self.assertTrue(result["verified"])
        self.assertTrue(result["expiry_already_passed"])
        self.assertTrue(result["recovery_only"])
        self.assertTrue(result["lease"]["expiry_already_passed"])
        self.assertLessEqual(datetime.fromisoformat(result["lease"]["guard_lease_until"]),NOW)
        self.assertEqual(datetime.fromisoformat(result["lease"]["guard_lease_until"]),
            receipt["created_at"]+timedelta(minutes=3))

    def test_current_publication_can_remain_disabled_during_past_write_recovery(self):
        state,receipt = reconciliation_inputs(expired=True)
        state["live_publication_enabled"] = False
        self.assertTrue(self.reconcile(state,receipt)["verified"])
        self.assertIs(state["live_publication_enabled"],False)

    def test_malformed_policy_value_is_not_treated_as_explicit_disabled_policy(self):
        for policy in (None,1,"false"):
            with self.subTest(policy=policy):
                state,receipt = reconciliation_inputs()
                state["live_publication_enabled"] = policy
                self.assertFalse(self.reconcile(state,receipt)["verified"])

    def test_supervised_actual_approval_and_same_approval_id_are_required(self):
        for changes in ({"supervised_live_approval":False},{"supervised_live_approval":1},
                        {"live_approval_id":"different-approval"}):
            with self.subTest(changes=changes):
                state,receipt = reconciliation_inputs()
                state.update(changes)
                result = self.reconcile(state,receipt)
                self.assertFalse(result["verified"])
                self.assertIsNone(result["ownership"])

    def test_http_failure_or_no_proven_dispatch_cannot_be_certified(self):
        for changes in ({"http_status":500},{"http_status":200.0},{"command_sent":False},
                        {"command_sent":1}):
            with self.subTest(changes=changes):
                state,receipt = reconciliation_inputs()
                receipt.update(changes)
                self.assertFalse(self.reconcile(state,receipt)["verified"])

    def test_explicit_rejection_is_never_reinterpreted_as_unknown_success(self):
        for changes in ({"write_accepted":False},{"result":False},{"write_result":False},
                        {"response":{"result":False}},{"code":400},{"explicit_failure":True}):
            with self.subTest(changes=changes):
                state,receipt = reconciliation_inputs()
                receipt.update(changes)
                result = self.reconcile(state,receipt)
                self.assertFalse(result["verified"])
                self.assertTrue(result["application_acknowledgment_known"])
                self.assertIsNone(result["lease"])

    def test_recognised_ack_must_use_normal_verification_not_unknown_ack_reconciliation(self):
        for changes in ({"write_accepted":True},{"code":0}):
            with self.subTest(changes=changes):
                state,receipt = reconciliation_inputs()
                receipt.update(changes)
                self.assertFalse(self.reconcile(state,receipt)["verified"])
        state,receipt = reconciliation_inputs()
        state["write_acknowledged"] = True
        self.assertFalse(self.reconcile(state,receipt)["verified"])

    def test_same_preexisting_hash_does_not_prove_a_supervised_transition(self):
        state,receipt = reconciliation_inputs()
        receipt["preflight_tariff_hash"] = "b"*64
        state["expected_baseline_hash"] = "b"*64
        result = self.reconcile(state,receipt)
        self.assertIn("distinct_preflight_transition_not_verified",result["reason_codes"])
        self.assertIsNone(result["ownership"])

    def test_mismatched_approved_baseline_or_target_cannot_be_adopted(self):
        for changes in ({"expected_baseline_hash":"c"*64},{"candidate_tariff_hash":"c"*64}):
            with self.subTest(changes=changes):
                state,receipt = reconciliation_inputs()
                state.update(changes)
                self.assertFalse(self.reconcile(state,receipt)["verified"])

    def test_first_or_second_hash_mismatch_blocks_records(self):
        state,receipt = reconciliation_inputs()
        receipt["first_readback_tariff_hash"] = "c"*64
        self.assertFalse(self.reconcile(state,receipt)["verified"])
        for changes in ({"fresh_readback_tariff_hash":"c"*64},):
            state,receipt = reconciliation_inputs()
            state.update(changes)
            self.assertFalse(self.reconcile(state,receipt)["verified"])
        state,receipt = reconciliation_inputs()
        state["readback"]["current_tariff_hash"] = "c"*64
        self.assertFalse(self.reconcile(state,receipt)["verified"])

    def test_independent_second_readback_must_be_explicit_actual_true_and_later(self):
        for independent in (False,None,1):
            with self.subTest(independent=independent):
                state,receipt = reconciliation_inputs()
                state["independent_readback_verified"] = independent
                self.assertFalse(self.reconcile(state,receipt)["verified"])
        state,receipt = reconciliation_inputs()
        state["readback"]["observed_at"] = receipt["first_readback_at"]
        self.assertFalse(self.reconcile(state,receipt)["verified"])

    def test_stale_source_eon_unknown_or_controller_change_never_certifies(self):
        for changes in ({"source_last_success":NOW-timedelta(minutes=15)},
                        {"frozen_assumption_approved":False},{"candidate_prices_verified":False}):
            with self.subTest(changes=changes):
                state,receipt = reconciliation_inputs()
                state.update(changes)
                self.assertFalse(self.reconcile(state,receipt)["verified"])
        for key,value in (("battery_enrolled",True),("battery_enrolled",None),
                          ("mode","self_consumption"),("energy_commands_granted",False),
                          ("enrolled_last_success",NOW-timedelta(minutes=45)),
                          ("live_last_success",NOW-timedelta(minutes=5))):
            with self.subTest(key=key,value=value):
                state,receipt = reconciliation_inputs()
                state["readback"][key] = value
                self.assertFalse(self.reconcile(state,receipt)["verified"])

    def test_changed_protected_settings_in_either_postflight_block_certification(self):
        for field in ("preflight_preserve_settings","first_readback_preserve_settings"):
            with self.subTest(field=field):
                state,receipt = reconciliation_inputs()
                receipt[field]["backup_reserve"] = 20
                self.assertFalse(self.reconcile(state,receipt)["verified"])
        state,receipt = reconciliation_inputs()
        state["preserve_settings_after"]["grid_charging"] = True
        self.assertFalse(self.reconcile(state,receipt)["verified"])

    def test_site_tamper_or_expired_quote_window_blocks_records(self):
        state,receipt = reconciliation_inputs()
        receipt["energy_site_id"] = 9
        self.assertFalse(self.reconcile(state,receipt)["verified"])
        state,receipt = reconciliation_inputs()
        state["readback"]["energy_site_id"] = 9
        self.assertFalse(self.reconcile(state,receipt)["verified"])
        state,receipt = reconciliation_inputs()
        state["known_valid_until"] = NOW
        self.assertFalse(self.reconcile(state,receipt)["verified"])

    def test_watchdog_health_is_real_prerequisite_not_fabricated_by_recovery(self):
        state,receipt = reconciliation_inputs(expired=True)
        state["watchdog"]["pid_alive"] = False
        self.assertFalse(self.reconcile(state,receipt)["verified"])
        self.assertIs(state["watchdog"]["pid_alive"],False)

    def test_reconciliation_changes_no_input_and_makes_no_ack_claim(self):
        state,receipt = reconciliation_inputs(expired=True)
        state_before,receipt_before = copy.deepcopy(state),copy.deepcopy(receipt)
        result = self.reconcile(state,receipt)
        self.assertTrue(result["verified"])
        self.assertEqual(state,state_before)
        self.assertEqual(receipt,receipt_before)
        self.assertFalse(result["ownership"]["write_acknowledged"])
        self.assertIsNone(result["ownership"]["write_accepted"])
        self.assertFalse(result["grid_charging_state_known"])


if __name__ == "__main__":
    unittest.main()
