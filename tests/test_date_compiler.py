import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from date_compiler import build_date_candidate
from tariff_schema import rate_at, to_upload_tariff, validate_tariff

UTC = timezone.utc


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def baseline(buy=0.30, sell=0.10):
    def channel(price):
        return {"currency": "GBP", "code": "BASELINE", "demand_charges": {"ALL": {"ALL": 0}},
                "energy_charges": {"Annual": {"Flat": price}},
                "seasons": {"Annual": {"fromMonth": 1, "fromDay": 1, "toMonth": 12, "toDay": 31,
                             "tou_periods": {"Flat": [{"fromDayOfWeek": 0, "toDayOfWeek": 6,
                             "fromHour": 0, "fromMinute": 0, "toHour": 0, "toMinute": 0}]}}}}
    result = channel(buy)
    result["sell_tariff"] = channel(sell)
    return result


def preview(start="2026-11-01T12:00:00Z", count=48, *, now=None, equal_prices=False):
    start = instant(start)
    now = now or start + timedelta(minutes=5)
    rows = []
    for index in range(count):
        buy = Decimal("0.4") if equal_prices else Decimal("0.4") + Decimal(index) / 1000
        sell = Decimal("0.2")
        lower = start + index * timedelta(minutes=30)
        upper = lower + timedelta(minutes=30)
        rows.append({"valid_from": lower.isoformat(), "valid_to": upper.isoformat(),
                     "import_gbp_per_kwh": str(buy), "export_gbp_per_kwh": str(sell),
                     "import_p_per_kwh": str(buy * 100), "export_p_per_kwh": str(sell * 100)})
    return {"valid": True, "errors": [], "currency": "GBP", "timezone": "Europe/London",
            "source_last_success": now.isoformat(), "source_captured_at": now.isoformat(),
            "periods": rows}, now


class DateCompilerTests(unittest.TestCase):
    def test_current_and_future_quotes_exact_both_directions(self):
        source, now = preview()
        result = build_date_candidate(source, baseline(), now=now)
        self.assertEqual(result["errors"], [])
        self.assertTrue(result["candidate_valid"])
        self.assertFalse(result["ready_for_upload"])
        for row in source["periods"]:
            stamp = instant(row["valid_from"])
            self.assertEqual(rate_at(result["tariff"], stamp), Decimal(row["import_gbp_per_kwh"]))
            self.assertEqual(rate_at(result["tariff"], stamp, "export"), Decimal(row["export_gbp_per_kwh"]))

    def test_tomorrow_quote_never_replaces_elapsed_today_clock(self):
        source, now = preview()
        result = build_date_candidate(source, baseline(), now=now)
        tariff = result["tariff"]
        self.assertEqual(rate_at(tariff, instant("2026-11-01T09:00:00Z")), Decimal("0.3"))
        self.assertEqual(rate_at(tariff, instant("2026-11-02T09:00:00Z")), Decimal("0.442"))
        self.assertFalse(result["manifest"]["elapsed_clock_prices_are_historical_optimise"])

    def test_all_unquoted_calendar_dates_preserve_baseline(self):
        source, now = preview()
        result = build_date_candidate(source, baseline(), now=now)
        for day in ("2026-01-01T07:15:00Z", "2028-02-29T12:30:00Z", "2026-12-31T23:59:00Z"):
            self.assertEqual(rate_at(result["tariff"], instant(day)), Decimal("0.3"))
        self.assertEqual(validate_tariff(result["tariff"]), [])

    def test_year_boundary_quotes_remain_on_correct_dates(self):
        source, now = preview("2026-12-31T23:30:00Z", 4)
        result = build_date_candidate(source, baseline(), now=now)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["manifest"]["quoted_dates"], ["2026-12-31", "2027-01-01"])
        self.assertEqual(rate_at(result["tariff"], instant("2026-12-31T00:00:00Z")), Decimal("0.3"))
        self.assertEqual(rate_at(result["tariff"], instant("2027-01-01T00:00:00Z")), Decimal("0.401"))

    def test_baseline_below_export_blocks_default_overlay(self):
        source, now = preview()
        result = build_date_candidate(source, baseline(0, 0.1), now=now)
        self.assertFalse(result["candidate_valid"])
        self.assertTrue(result["tariff"])
        self.assertIn("candidate_tesla_validation_failed", [x["code"] for x in result["errors"]])
        self.assertEqual(rate_at(result["tariff"], instant("2026-11-01T09:00:00Z")), Decimal(0))

    def test_zero_placeholder_requires_explicit_policy_and_guard(self):
        source, now = preview()
        result = build_date_candidate(source, baseline(0, 0.1), now=now,
                                      unquoted_policy="disabled_zero_placeholder")
        self.assertEqual(result["errors"], [])
        self.assertTrue(result["candidate_valid"])
        self.assertFalse(result["ready_for_upload"])
        self.assertEqual(rate_at(result["tariff"], instant("2026-11-01T09:00:00Z")), Decimal(0))
        self.assertEqual(rate_at(result["tariff"], instant("2026-11-01T09:00:00Z"), "export"), Decimal(0))
        self.assertTrue(result["manifest"]["unquoted_intervals_must_not_drive_savings"])
        self.assertFalse(result["manifest"]["unquoted_prices_are_optimise_quotes"])
        self.assertIn("expiry_self_powered_handoff", result["manifest"]["required_approvals"])
        self.assertTrue(result["manifest"]["unquoted_zero_prices_can_influence_savings_lookahead"])
        self.assertIn("zero_placeholder_lookahead_unapproved", result["manifest"]["activation_blockers"])

    def test_frozen_current_pair_preserves_all_quotes_exactly(self):
        source, now = preview()
        result = build_date_candidate(source, baseline(0, 0.1), now=now,
                                      unquoted_policy="frozen_current_quote")
        self.assertEqual(result["errors"], [])
        self.assertTrue(result["candidate_valid"])
        for row in source["periods"]:
            stamp = instant(row["valid_from"])
            self.assertEqual(rate_at(result["tariff"], stamp), Decimal(row["import_gbp_per_kwh"]))
            self.assertEqual(rate_at(result["tariff"], stamp, "export"), Decimal(row["export_gbp_per_kwh"]))

    def test_frozen_assumption_uses_current_pair_not_next_day_pair(self):
        source, now = preview()
        result = build_date_candidate(source, baseline(), now=now,
                                      unquoted_policy="frozen_current_quote")
        # The elapsed clock's replacement is explicitly an assumption, not a
        # remapping of tomorrow's different 09:00 quote or a historical claim.
        self.assertEqual(rate_at(result["tariff"], instant("2026-11-01T09:00:00Z")), Decimal("0.4"))
        self.assertEqual(rate_at(result["tariff"], instant("2026-11-02T09:00:00Z")), Decimal("0.442"))
        self.assertEqual(rate_at(result["tariff"], instant("2026-11-03T09:00:00Z")), Decimal("0.4"))
        manifest = result["manifest"]
        self.assertEqual(manifest["unquoted_assumed_pair"]["import_p_per_kwh"], "40.0")
        self.assertTrue(manifest["unquoted_static_pair_is_observed_current_quote"])
        self.assertFalse(manifest["unquoted_static_pair_is_future_forecast"])
        self.assertFalse(manifest["unquoted_static_pair_is_historical_price"])
        self.assertFalse(manifest["elapsed_clock_prices_are_historical_optimise"])

    def test_frozen_policy_still_needs_approval_and_expiry_handoff(self):
        source, now = preview()
        result = build_date_candidate(source, baseline(), now=now,
                                      unquoted_policy="frozen_current_quote")
        self.assertFalse(result["ready_for_upload"])
        self.assertTrue(result["manifest"]["unquoted_lookahead_assumption_requires_approval"])
        self.assertIn("unquoted_lookahead_assumption", result["manifest"]["required_approvals"])
        self.assertIn("expiry_self_powered_handoff", result["manifest"]["required_approvals"])
        self.assertIn("expiry_handoff_unapproved", result["manifest"]["activation_blockers"])

    def test_spring_jump_preserves_nonexistent_clock_as_unquoted(self):
        source, now = preview("2027-03-28T00:00:00Z", 8)
        result = build_date_candidate(source, baseline(), now=now)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["manifest"]["nonexistent_local_clock_intervals"],
                         [{"date": "2027-03-28", "from_minute": 60, "to_minute": 120}])
        for row in source["periods"]:
            self.assertEqual(rate_at(result["tariff"], instant(row["valid_from"])),
                             Decimal(row["import_gbp_per_kwh"]))

    def test_fall_repeated_hour_distinct_rates_blocked(self):
        source, now = preview("2026-10-25T00:00:00Z", 8)
        result = build_date_candidate(source, baseline(), now=now)
        self.assertIsNone(result["tariff"])
        self.assertIn("ambiguous_dst_rates", [x["code"] for x in result["errors"]])

    def test_fall_repeated_hour_equal_rates_supported(self):
        source, now = preview("2026-10-25T00:00:00Z", 8, equal_prices=True)
        result = build_date_candidate(source, baseline(), now=now)
        self.assertEqual(result["errors"], [])
        for row in source["periods"]:
            self.assertEqual(rate_at(result["tariff"], instant(row["valid_from"])), Decimal("0.4"))

    def test_partial_future_fold_cannot_overwrite_elapsed_fold(self):
        source, now = preview("2026-10-25T01:00:00Z", 6, equal_prices=True)
        result = build_date_candidate(source, baseline(), now=now)
        self.assertIsNone(result["tariff"])
        self.assertIn("partial_dst_fold_coverage", [x["code"] for x in result["errors"]])

    def test_stale_source_blocked_at_fifteen_minutes(self):
        source, now = preview()
        source["source_last_success"] = (now - timedelta(minutes=15)).isoformat()
        result = build_date_candidate(source, baseline(), now=now)
        self.assertIsNone(result["tariff"])
        self.assertIn("source_not_fresh", [x["code"] for x in result["errors"]])

    def test_gap_overlap_and_unpaired_units_blocked(self):
        source, now = preview()
        source["periods"].pop(2)
        result = build_date_candidate(source, baseline(), now=now)
        self.assertIn("quote_gap_or_overlap", [x["code"] for x in result["errors"]])
        source, now = preview()
        source["periods"][1]["import_p_per_kwh"] = "1"
        result = build_date_candidate(source, baseline(), now=now)
        self.assertIn("inconsistent_money_units", [x["code"] for x in result["errors"]])

    def test_negative_and_sell_above_buy_never_clamped(self):
        source, now = preview()
        source["periods"][0]["import_gbp_per_kwh"] = "-0.1"
        result = build_date_candidate(source, baseline(), now=now)
        self.assertIsNone(result["tariff"])
        source, now = preview()
        source["periods"][0]["export_gbp_per_kwh"] = "0.9"
        result = build_date_candidate(source, baseline(), now=now)
        self.assertIn("export_exceeds_import", [x["code"] for x in result["errors"]])

    def test_inputs_are_not_mutated_and_post_rates_wrapper_present(self):
        source, now = preview()
        base = baseline()
        source_before, base_before = copy.deepcopy(source), copy.deepcopy(base)
        result = build_date_candidate(source, base, now=now)
        self.assertEqual(source, source_before)
        self.assertEqual(base, base_before)
        for part in (result["tariff"], result["tariff"]["sell_tariff"]):
            self.assertTrue(all(set(value) == {"rates"} for value in part["energy_charges"].values()))

    def test_documented_post_baseline_wrappers_supported(self):
        source, now = preview()
        result = build_date_candidate(source, to_upload_tariff(baseline()), now=now)
        self.assertEqual(result["errors"], [])
        self.assertEqual(rate_at(result["tariff"], instant("2026-11-01T09:00:00Z")), Decimal("0.3"))


if __name__ == "__main__":
    unittest.main()
