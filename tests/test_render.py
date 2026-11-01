"""Renderer tests use synthetic data only; no files, credentials or services."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from optimise_tariff_preview.render import render_html


def sample():
    return {
        "generated_at": "2026-11-01T11:29:03Z",
        "as_of": "2026-11-01T11:29:03Z",
        "source_captured_at": "2026-11-01T11:28:03Z",
        "source_last_success": "2026-11-01T11:26:33Z",
        "timezone": "Europe/London",
        "valid": True,
        "ready_for_upload": False,
        "errors": [],
        "warnings": [{"code": "forecast", "message": "Forecast, not final billing data"}],
        "coverage": {"start": "2026-11-01T11:00:00Z", "end": "2026-11-01T12:00:00Z", "period_count": 2},
        "periods": [
            {"valid_from": "2026-11-01T11:00:00Z", "valid_to": "2026-11-01T11:30:00Z", "import_p_per_kwh": "24", "export_p_per_kwh": "20", "import_gbp_per_kwh": "0.24", "export_gbp_per_kwh": "0.20", "import_quality": "forecast", "export_quality": "forecast", "import_estimate": False, "export_estimate": False, "import_interval_basis": "provisional_30m", "export_interval_basis": "provisional_30m", "compatible": True},
            {"valid_from": "2026-11-01T11:30:00Z", "valid_to": "2026-11-01T12:00:00Z", "import_p_per_kwh": "0", "export_p_per_kwh": None, "import_gbp_per_kwh": "0", "export_gbp_per_kwh": None, "compatible": False},
        ],
    }


class RenderTests(unittest.TestCase):
    def test_inert_standalone_document(self):
        page = render_html(sample())
        self.assertIn("DRY RUN — NO TESLA WRITES", page)
        self.assertIn("default-src 'none'", page)
        self.assertNotIn("<script", page.lower())
        self.assertNotIn("https://", page)
        self.assertNotIn("http://", page)
        self.assertNotIn("<form", page.lower())
        self.assertNotIn("<iframe", page.lower())

    def test_provider_text_is_escaped(self):
        report = sample()
        attack = '<img src=x onerror="alert(1)">'
        report["errors"] = [{"code": attack, "message": "<&unsafe>", "channel": attack}]
        report["source"] = {"label": attack}
        report["periods"][0]["import_quality"] = attack
        page = render_html(report)
        self.assertNotIn(attack, page)
        self.assertIn("&lt;img", page)
        self.assertIn("&lt;&amp;unsafe&gt;", page)
        self.assertNotIn("<img", page)

    def test_zero_is_not_missing(self):
        page = render_html(sample())
        self.assertIn("<td>0.00</td>", page)
        self.assertIn("<td>0.0000</td>", page)
        self.assertIn("<td>—</td>", page)

    def test_exact_unit_values_and_quality(self):
        page = render_html(sample())
        self.assertIn("<td>24.00</td>", page)
        self.assertIn("<td>0.2400</td>", page)
        self.assertIn("forecast / forecast", page)
        self.assertIn("No / No", page)
        self.assertIn("provisional_30m / provisional_30m", page)
        self.assertIn("1.0 minutes", page)
        self.assertIn("2.5 minutes", page)

    def test_bad_intervals_are_highlighted(self):
        report = sample()
        report["periods"][0]["import_p_per_kwh"] = "-3.5"
        report["periods"][0]["compatible"] = True
        page = render_html(report)
        self.assertIn("<td>-3.50</td>", page)
        self.assertEqual(page.count("<tr class=\"bad\">"), 2)
        self.assertIn("fill=\"#fce3e3\"", page)

    def test_unknown_or_missing_report_is_safe(self):
        page = render_html({})
        self.assertIn("No aligned periods", page)
        self.assertIn("No valid timed price values", page)
        self.assertIn("REVIEW", page)
        self.assertIn("No candidate Tesla payload", page)
        self.assertNotIn('class="state" class=', page)

    def test_invalid_times_and_nonfinite_prices_do_not_crash(self):
        report = sample()
        report["timezone"] = "Not/AZone"
        report["periods"][0]["valid_from"] = "not a date"
        report["periods"][0]["import_p_per_kwh"] = "NaN"
        report["periods"][0]["export_p_per_kwh"] = "Infinity"
        page = render_html(report)
        self.assertIn("unavailable; times shown in UTC", page)
        self.assertIn("<td>—</td>", page)

    def test_candidate_repeat_warning_is_prominent(self):
        report = sample()
        report["tesla_payload"] = {"code": "DRY_RUN"}
        report["payload_manifest"] = {"repeats_daily": True, "partial_history_remapped": True}
        report["upload_blockers"] = [{"code": "dry_run", "message": "Approval required"}]
        page = render_html(report)
        self.assertIn("candidate repeats its time-of-day schedule daily", page)
        self.assertIn("rolling horizon", page)
        self.assertIn("Approval required", page)
        self.assertIn("BLOCKED", page)

    def test_repeated_local_hour_disambiguated_by_zone(self):
        report = sample()
        report["periods"][0].update(valid_from="2026-10-25T00:00:00Z", valid_to="2026-10-25T00:30:00Z")
        report["periods"][1].update(valid_from="2026-10-25T01:00:00Z", valid_to="2026-10-25T01:30:00Z")
        page = render_html(report)
        self.assertIn("25 Oct 2026 01:00 BST", page)
        self.assertIn("25 Oct 2026 01:00 GMT", page)

    def test_extreme_zero_exponent_does_not_expand_output(self):
        report = sample()
        report["periods"][0]["import_p_per_kwh"] = "0E+999999999"
        page = render_html(report)
        self.assertIn("<td>0.00</td>", page)
        self.assertLess(len(page), 50000)


if __name__ == "__main__":
    unittest.main()
