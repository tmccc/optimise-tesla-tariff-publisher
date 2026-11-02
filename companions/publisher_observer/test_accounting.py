"""Offline tests for measured accounting and illustrative storage modelling."""
import math
import unittest

from accounting import summarize_window, simulate_self_powered


def segment(start=0, end=3600, **overrides):
    row = dict(start=start, end=end, grid_import_kwh=0.0, grid_export_kwh=0.0,
               battery_charge_kwh=0.0, battery_discharge_kwh=0.0,
               solar_kwh=0.0, home_kwh=0.0,
               optimise_import_p=20.0, optimise_export_p=65.0,
               agile_import_p=25.0, agile_export_p=15.0, quality="monitored")
    row.update(overrides)
    return row


class MeasuredAccountingTests(unittest.TestCase):
    def test_costs_directional_not_net_energy_and_agile_is_same_trace(self):
        row = segment(grid_import_kwh=2, grid_export_kwh=1, home_kwh=3)
        result = summarize_window([row], 0, 3600)
        self.assertAlmostEqual(result["import_cost_gbp"], .4)
        self.assertAlmostEqual(result["export_revenue_gbp"], .65)
        self.assertAlmostEqual(result["net_cost_gbp"], -.25)
        self.assertAlmostEqual(result["same_trace_agile_net_cost_gbp"], .35)
        self.assertAlmostEqual(result["same_trace_agile_minus_optimise_net_cost_gbp"], .6)
        self.assertIn("not_controller_savings", result["comparison_basis"])

    def test_partial_prices_do_not_invent_revenue(self):
        result = summarize_window([segment(grid_import_kwh=2, grid_export_kwh=1,
                                          optimise_export_p=None)], 0, 3600)
        self.assertEqual(result["net_cost_gbp"], .4)
        self.assertEqual(result["unpriced_export_kwh"], 1)
        self.assertEqual(result["priced_coverage_percent"], 0)
        self.assertTrue(result["net_cost_is_partial"])
        self.assertIsNone(result["same_trace_agile_minus_optimise_net_cost_gbp"])

    def test_negative_prices_preserve_sign(self):
        result = summarize_window([segment(grid_import_kwh=2, grid_export_kwh=1,
                                          optimise_import_p=-10, optimise_export_p=-5)], 0, 3600)
        self.assertAlmostEqual(result["net_cost_gbp"], -.15)

    def test_clipping_elapsed_horizon(self):
        result = summarize_window([segment(grid_import_kwh=4)], 900, 7200, now=2700)
        self.assertEqual(result["grid_import_kwh"], 2)
        self.assertEqual(result["expected_hours"], .5)
        self.assertEqual(result["coverage_percent"], 100)

    def test_missing_samples_are_not_zero_energy(self):
        result = summarize_window([segment(end=1800, grid_import_kwh=1)], 0, 3600)
        self.assertEqual(result["grid_import_kwh"], 1)
        self.assertEqual(result["coverage_percent"], 50)
        self.assertEqual(result["unmeasured_hours"], .5)
        self.assertTrue(result["net_cost_is_partial"])

    def test_overlap_cannot_double_count(self):
        rows = [segment(grid_import_kwh=2), segment(1800, 5400, grid_import_kwh=4)]
        result = summarize_window(rows, 0, 5400)
        self.assertEqual(result["grid_import_kwh"], 4)
        self.assertEqual(result["coverage_percent"], 100)
        self.assertEqual(result["overlap_hours_ignored"], .5)

    def test_unverified_trace_is_distinct_from_measured_coverage(self):
        result = summarize_window([segment(quality="unverified")], 0, 3600)
        self.assertEqual(result["coverage_percent"], 100)
        self.assertEqual(result["priced_coverage_percent"], 100)
        self.assertEqual(result["monitored_coverage_percent"], 0)

    def test_invalid_energy_is_not_silently_integrated(self):
        result = summarize_window([segment(grid_import_kwh=-1), segment(grid_import_kwh=math.nan)], 0, 3600)
        self.assertEqual(result["invalid_segment_count"], 2)
        self.assertEqual(result["coverage_percent"], 0)

    def test_before_start_no_invented_coverage(self):
        result = summarize_window([segment()], 0, 3600, now=-10)
        self.assertEqual(result["expected_hours"], 0)
        self.assertEqual(result["coverage_percent"], 0)
        self.assertTrue(result["net_cost_is_partial"])

    def test_nonfinite_price_is_unpriced(self):
        result = summarize_window([segment(grid_import_kwh=1, optimise_import_p=math.inf)], 0, 3600)
        self.assertEqual(result["unpriced_import_kwh"], 1)


class CounterfactualTests(unittest.TestCase):
    def model(self, rows, **kwargs):
        params = dict(initial_stored_kwh=5, capacity_kwh=10, reserve_floor_kwh=1,
                      roundtrip_efficiency=1, max_power_kw=10, actual_final_stored_kwh=5)
        params.update(kwargs)
        return simulate_self_powered(rows, 0, 3600, **params)

    def test_reserve_limits_discharge_no_deliberate_export(self):
        result = self.model([segment(home_kwh=8, grid_import_kwh=8)])
        self.assertTrue(result["eligible"])
        self.assertEqual(result["model"]["battery_discharge_kwh"], 4)
        self.assertEqual(result["model"]["grid_import_kwh"], 4)
        self.assertEqual(result["model"]["grid_export_kwh"], 0)
        self.assertEqual(result["model_ending_stored_kwh"], 1)

    def test_solar_charging_is_capacity_limited(self):
        result = self.model([segment(solar_kwh=9, home_kwh=1)], initial_stored_kwh=9)
        self.assertEqual(result["model"]["battery_charge_kwh"], 1)
        self.assertEqual(result["model"]["grid_export_kwh"], 7)
        self.assertEqual(result["model_ending_stored_kwh"], 10)

    def test_no_grid_charging_with_negative_price(self):
        result = self.model([segment(home_kwh=2, optimise_import_p=-20)], initial_stored_kwh=1)
        self.assertEqual(result["model"]["battery_charge_kwh"], 0)
        self.assertEqual(result["model"]["grid_import_kwh"], 2)

    def test_efficiency_and_power_limit(self):
        result = self.model([segment(home_kwh=8)], roundtrip_efficiency=.81, max_power_kw=2)
        self.assertEqual(result["model"]["battery_discharge_kwh"], 2)
        self.assertAlmostEqual(result["model_ending_stored_kwh"], 5 - 2 / .9)

    def test_cash_and_terminal_inventory_sign(self):
        # Actual exported one kWh, ending depleted. Model holds its energy.
        result = self.model([segment(grid_export_kwh=1)], actual_final_stored_kwh=4)
        self.assertAlmostEqual(result["unadjusted_cash_difference_gbp"], .65)
        self.assertEqual(result["ending_inventory_delta_kwh"], -1)
        self.assertAlmostEqual(result["adjusted_estimates"][2]["cash_difference_gbp"], 0)
        self.assertFalse(result["native_inventory_freshness_verified"])

    def test_absent_final_inventory_has_no_invented_adjustment(self):
        result = self.model([segment()], actual_final_stored_kwh=None)
        self.assertIsNone(result["ending_inventory_delta_kwh"])
        self.assertEqual(result["adjusted_estimates"], [])

    def test_each_coverage_gate(self):
        for row in (segment(end=3500), segment(optimise_import_p=None), segment(quality="unverified")):
            with self.subTest(row=row):
                result = self.model([row])
                self.assertFalse(result["eligible"])
                self.assertNotIn("model", result)

    def test_one_percent_missing_is_allowed_but_explicitly_partial(self):
        result = self.model([segment(end=3564)])
        self.assertTrue(result["eligible"])
        self.assertTrue(result["monetary_estimates_are_partial"])

    def test_overlapping_data_aborts_counterfactual(self):
        result = self.model([segment(), segment()])
        self.assertFalse(result["eligible"])
        self.assertIn("overlapping_segments_present", result["reason_codes"])

    def test_invalid_physics_rejected(self):
        with self.assertRaises(ValueError):
            self.model([segment()], roundtrip_efficiency=1.1)


if __name__ == "__main__":
    unittest.main()
