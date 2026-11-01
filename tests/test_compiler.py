"""Independent boundary and money fixtures for the non-writing compiler."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import unittest

from optimise_tariff_preview.compiler import compile_preview

UTC = timezone.utc


def fixture(start=None, count=49, now=None):
    start = start or datetime(2026, 11, 1, 11, tzinfo=UTC)
    now = now or start + timedelta(minutes=29)
    channels = {}
    for channel, value in [("import", "24"), ("export", "20")]:
        channels[channel] = [
            {"valid_from": (start+timedelta(minutes=30*i)).isoformat(),
             "valid_to": (start+timedelta(minutes=30*(i+1))).isoformat(),
             "value": value, "quality": "current" if i == 0 else "forecast",
             "estimate": False, "interval_basis": "provisional_30m"}
            for i in range(count)
        ]
    return {"captured_at": now.isoformat(), "now": now.isoformat(),
            "last_success": (now-timedelta(minutes=2)).isoformat(),
            "timezone": "Europe/London", "unit": "p/kWh", "currency": "GBP",
            "channels": channels}


def codes(report, key="errors"):
    return {x["code"] for x in report[key]}


class CompilerTests(unittest.TestCase):
    def test_valid_preview_builds_candidate_but_never_becomes_live(self):
        report = compile_preview(fixture())
        self.assertTrue(report["valid"])
        self.assertFalse(report["ready_for_upload"])
        self.assertTrue(report["candidate_payload_generated"])
        self.assertEqual(report["coverage"]["period_count"], 49)
        self.assertEqual(report["periods"][0]["import_gbp_per_kwh"], "0.24")
        self.assertEqual(report["periods"][0]["export_gbp_per_kwh"], "0.2")
        self.assertEqual(report["summary"]["tesla_writes"], 0)
        json.dumps(report, allow_nan=False)

    def test_export_earnings_are_not_negated_again(self):
        report = compile_preview(fixture())
        self.assertEqual(report["periods"][0]["export_p_per_kwh"], "20")
        self.assertEqual(report["tesla_payload"]["sell_tariff"]["energy_charges"]["HA_DAILY"]["rates"]["HA_1100"], 0.2)

    def test_negative_prices_preserved_and_blocked(self):
        source = fixture()
        source["channels"]["import"][0]["value"] = "-2.5"
        report = compile_preview(source)
        self.assertIn("negative_price", codes(report))
        self.assertFalse(report["candidate_payload_valid"])
        self.assertEqual(report["periods"][0]["import_p_per_kwh"], "-2.5")
        self.assertEqual(report["tesla_payload"]["energy_charges"]["HA_DAILY"]["rates"]["HA_1100"], -0.025)

    def test_crossed_prices_are_not_silently_adjusted(self):
        source = fixture()
        source["channels"]["export"][3]["value"] = "28"
        report = compile_preview(source)
        self.assertIn("export_exceeds_import", codes(report))
        self.assertFalse(report["periods"][3]["compatible"])
        self.assertEqual(report["tesla_payload"]["sell_tariff"]["energy_charges"]["HA_DAILY"]["rates"]["HA_1230"], 0.28)
        self.assertEqual(report["tesla_payload"]["energy_charges"]["HA_DAILY"]["rates"]["HA_1230"], 0.24)

    def test_stale_success_is_blocked(self):
        source = fixture()
        source["last_success"] = (datetime.fromisoformat(source["now"])-timedelta(minutes=15)).isoformat()
        self.assertIn("stale_last_success", codes(compile_preview(source)))

    def test_snapshot_cannot_relabel_old_data_as_current(self):
        source = fixture()
        report = compile_preview(source, now=datetime.fromisoformat(source["now"])+timedelta(minutes=16))
        self.assertIn("stale_captured_at", codes(report))
        self.assertIn("stale_last_success", codes(report))

    def test_boolean_missing_and_nonfinite_prices_rejected(self):
        for value in [True, None, "NaN", "Infinity", float("nan")]:
            with self.subTest(value=repr(value)):
                source = fixture()
                source["channels"]["import"][1]["value"] = value
                self.assertIn("invalid_price", codes(compile_preview(source)))

    def test_gap_not_filled_with_zero(self):
        source = fixture()
        del source["channels"]["import"][3]
        del source["channels"]["export"][3]
        report = compile_preview(source)
        self.assertIn("gap_in_periods", codes(report))
        self.assertEqual(len(report["periods"]), 48)
        self.assertIsNone(report["tesla_payload"])

    def test_import_export_pairing_is_exact(self):
        source = fixture()
        del source["channels"]["export"][3]
        self.assertIn("unpaired_channels", codes(compile_preview(source)))

    def test_overlaps_are_blocked(self):
        source = fixture()
        source["channels"]["import"].insert(3, deepcopy(source["channels"]["import"][2]))
        self.assertIn("overlapping_periods", codes(compile_preview(source)))

    def test_naive_timestamps_and_missing_end_rejected(self):
        for field, value in [("valid_from", "2026-11-01T11:30:00"), ("valid_to", None)]:
            source = fixture()
            source["channels"]["import"][1][field] = value
            self.assertIn("invalid_interval", codes(compile_preview(source)))

    def test_forecast_and_provisional_intervals_remain_labelled(self):
        report = compile_preview(fixture())
        self.assertIn("forecast_not_settlement", codes(report, "warnings"))
        self.assertIn("provisional_interval_ends", codes(report, "warnings"))
        self.assertEqual(report["periods"][1]["import_quality"], "forecast")
        self.assertFalse(report["periods"][1]["import_estimate"])

    def test_unknown_interval_basis_is_not_assumed_to_be_provider_end(self):
        source = fixture()
        source["channels"]["import"][0]["interval_basis"] = None
        self.assertIn("unknown_interval_basis", codes(compile_preview(source)))

    def test_units_are_not_guessed(self):
        source = fixture()
        source["unit"] = "GBP/kWh"
        report = compile_preview(source)
        self.assertIn("unsupported_unit", codes(report))
        self.assertIsNone(report["tesla_payload"])
        self.assertIsNone(report["periods"][0]["import_gbp_per_kwh"])

    def test_extreme_decimal_exponents_are_rejected_without_arithmetic_overflow(self):
        for value in ["1e999999999999999999", "1e-999999999999999999"]:
            source = fixture()
            source["channels"]["import"][0]["value"] = value
            self.assertIn("invalid_price", codes(compile_preview(source)))

    def test_exact_decimal_pence_conversion(self):
        source = fixture()
        source["channels"]["import"][0]["value"] = Decimal("24.123456")
        report = compile_preview(source)
        self.assertEqual(report["periods"][0]["import_gbp_per_kwh"], "0.24123456")

    def test_daily_payload_full_clock_coverage_and_midnight(self):
        report = compile_preview(fixture())
        payload = report["tesla_payload"]
        self.assertEqual(payload["version"], 1)
        season = payload["seasons"]["HA_DAILY"]
        self.assertEqual((season["fromMonth"], season["fromDay"], season["toMonth"], season["toDay"]), (1, 1, 12, 31))
        self.assertEqual(len(season["tou_periods"]), 48)
        self.assertEqual(season["tou_periods"]["HA_2330"]["periods"][0]["toHour"], 0)
        self.assertEqual(season["tou_periods"]["HA_2330"]["periods"][0]["toMinute"], 0)
        self.assertEqual(payload["sell_tariff"]["currency"], "GBP")
        self.assertIn("daily_template_repeats_beyond_known_horizon", codes(report, "warnings"))
        self.assertTrue(report["payload_manifest"]["historical_clock_prices_may_be_remapped"])

    def test_short_horizon_cannot_generate_full_daily_template(self):
        report = compile_preview(fixture(count=12))
        self.assertIsNone(report["tesla_payload"])
        self.assertIn("daily_template_not_representable", codes(report, "upload_blockers"))

    def test_uk_fall_day_is_50_real_half_hours(self):
        start = datetime(2026, 10, 24, 23, tzinfo=UTC)
        source = fixture(start, count=50, now=start)
        source["channels"]["import"][4]["value"] = "30"
        report = compile_preview(source)
        self.assertTrue(report["coverage"]["full_today"])
        self.assertEqual(report["coverage"]["local_days"][0]["expected_periods"], 50)
        self.assertIn("ambiguous_dst_rates", codes(report))
        self.assertIsNone(report["tesla_payload"])

    def test_uk_spring_day_is_46_real_half_hours(self):
        start = datetime(2026, 3, 29, tzinfo=UTC)
        report = compile_preview(fixture(start, count=46, now=start))
        self.assertTrue(report["coverage"]["full_today"])
        self.assertEqual(report["coverage"]["local_days"][0]["expected_periods"], 46)
        self.assertIsNone(report["tesla_payload"])

    def test_input_is_not_modified(self):
        source = fixture()
        original = deepcopy(source)
        compile_preview(source)
        self.assertEqual(source, original)


if __name__ == "__main__":
    unittest.main()
