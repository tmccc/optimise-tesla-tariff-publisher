"""Offline observer/store contracts. No credentials, HTTP, or live HA writes."""
import contextlib
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import observer
from observation_store import POWER_FIELDS, Store, integrate_pair, iso


T = 1577836800  # A past UTC half-hour boundary; no dependency on live config.


def sample(ts, **overrides):
    row = {"ts": ts, **{key + "_w": 0.0 for key in POWER_FIELDS}, "soe": 50.0}
    row.update(grid_import_w=1000.0, home_w=1000.0)
    row.update(overrides)
    return row


def observation(ts, fresh=True):
    return {
        "captured_at": ts, "source_fresh": fresh, "source_timestamp": ts - 1,
        "source_age_seconds": 1.0, "monitor_updated_at": iso(ts), "errors": {},
        "inventory": {"remaining_kwh": 10.0 - (ts - T) / 3600,
                      "full_pack_kwh": 27.0, "soc_percent": 50.0,
                      "raw_soc_percent": 52.5, "max_charge_kw": 11.0,
                      "max_discharge_kw": 11.0,
                      "native_inventory_fetch_time_unverified": True},
        "counters": {"site": {"energy_imported": 10**12, "energy_exported": 10**12}},
        "gateway": {"real_mode": "autonomous", "backup_reserve_percent": 10,
                    "grid_charging": True, "grid_export": "battery_ok", "stale": False},
    }


class OfflineCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="observer-test-")
        self.addCleanup(self.temporary.cleanup)
        self.network = patch.object(observer, "get_json", side_effect=AssertionError("HTTP forbidden in offline tests"))
        self.http = self.network.start()
        self.addCleanup(self.network.stop)
        self.config = {
            "state_dir": self.temporary.name,
            "hass_url": "http://127.0.0.1:8123",
            "token_file": "/never-read-a-real-credential",
            "record_from": iso(T), "capture_until": iso(T + 3600),
            "windows": {"overall": [iso(T), iso(T + 3600)],
                        "overnight": [iso(T), iso(T + 1800)],
                        "peak": [iso(T + 1800), iso(T + 3600)]},
            "entities": {key: "sensor.test_" + key for key in (
                "prices_success", "status_success", "battery_enrolled",
                "optimise_import", "optimise_export", "comparison")},
        }

    def make_observer(self, **changes):
        config = dict(self.config, **changes)
        result = observer.Observer(config)
        self.addCleanup(result.store.db.close)
        return result

    def wire_cycle(self, instance):
        """Keep the real cycle, capture, accounting, report and publish paths."""
        instance.ha = Mock(return_value={})
        instance.snapshot = Mock(side_effect=observation)

        def health(now, obs, prices, status, enrolled):
            return "healthy", {
                "updated_at": iso(now),
                "publisher": {"last_cycle": iso(now), "last_verified_publication": iso(now),
                              "lease_expires": iso(now + 300), "pause_latch": None},
                "watchdog": {"last_seen": iso(now)},
                "gateway": {"actual_mode": "Savings", "backup_reserve_percent": 10},
                "eon": {"enrolled": False, "fresh": True}, "prices": {},
            }

        instance.health = Mock(side_effect=health)

        def state(key):
            now = observer.time.time()
            begin = int(now // 1800) * 1800
            if key in ("prices_success", "status_success"):
                return {"state": iso(now)}
            if key == "battery_enrolled":
                return {"state": "off"}
            if key in ("optimise_import", "optimise_export"):
                return {"state": "20", "last_updated": iso(begin), "attributes": {
                    "unit_of_measurement": "p/kWh", "period": {
                        "valid_from": iso(begin), "valid_to": iso(begin + 1800),
                        "value": 20 if key.endswith("import") else 65}}}
            if key == "comparison":
                return {"attributes": {"sources": {
                    "agile_import": {"last_success": iso(now)},
                    "agile_export": {"last_success": iso(now)}}, "rows": [
                        {"start": iso(begin), "end": iso(begin + 1800),
                         "agile_import": 25, "agile_export": 15}]}}
            raise AssertionError("Unexpected offline state: " + key)

        instance.state = Mock(side_effect=state)

        def raw_http(url, *args, **kwargs):
            self.assertFalse(args or kwargs, "Raw reads must carry no token/body")
            parsed = urlsplit(url)
            self.assertEqual(parsed.scheme + "://" + parsed.netloc, observer.GATEWAY)
            self.assertEqual(parsed.path, "/api/timeseries/samples")
            query = parse_qs(parsed.query)
            self.assertEqual(query["gateway"], ["default"])
            left, right = float(query["start"][0]), float(query["end"][0])
            rows = [sample(ts) for ts in range(T - 10, int(instance.until) + 1, 5) if left <= ts <= right]
            return {"enabled": True, "samples": rows}

        self.http.side_effect = raw_http

    def cycle_at(self, instance, now):
        with patch.object(observer.time, "time", return_value=now), contextlib.redirect_stdout(io.StringIO()):
            instance.cycle()


class IntegrationTests(OfflineCase):
    def test_five_second_watts_become_directional_kwh(self):
        row = integrate_pair(sample(T), sample(T + 5), T)[0]
        self.assertAlmostEqual(row["grid_import_kwh"], 1000 * 5 / 3_600_000)
        self.assertAlmostEqual(row["home_kwh"], 1000 * 5 / 3_600_000)
        self.assertEqual(row["grid_export_kwh"], 0)

    def test_half_hour_boundary_splits_linear_power_without_losing_energy(self):
        rows = integrate_pair(sample(T - 5, grid_import_w=0),
                              sample(T + 5, grid_import_w=2000), T - 5)
        self.assertEqual([(r["start"], r["end"]) for r in rows], [(T - 5, T), (T, T + 5)])
        self.assertAlmostEqual(rows[0]["grid_import_kwh"], 500 * 5 / 3_600_000)
        self.assertAlmostEqual(rows[1]["grid_import_kwh"], 1500 * 5 / 3_600_000)
        self.assertAlmostEqual(sum(r["grid_import_kwh"] for r in rows), 1000 * 10 / 3_600_000)

    def test_record_start_clips_interpolated_energy(self):
        rows = integrate_pair(sample(T - 5, grid_import_w=0),
                              sample(T + 5, grid_import_w=2000), T)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["start"], T)
        self.assertAlmostEqual(rows[0]["grid_import_kwh"], 1500 * 5 / 3_600_000)

    def test_gap_over_thirty_seconds_is_missing_not_zero_energy(self):
        self.assertEqual(integrate_pair(sample(T), sample(T + 31), T), [])
        self.assertEqual(len(integrate_pair(sample(T), sample(T + 30), T)), 1)
        self.assertEqual(integrate_pair(sample(T), sample(T), T), [])
        self.assertEqual(integrate_pair(sample(T + 5), sample(T), T), [])

    def test_invalid_directional_power_rejected(self):
        for value in (-1, float("nan"), float("inf"), True, None):
            with self.subTest(value=value):
                self.assertEqual(integrate_pair(sample(T, grid_import_w=value), sample(T + 5), T), [])

    def test_duplicate_and_unsorted_samples_cannot_double_count(self):
        s = self.make_observer().store
        points = [sample(T + 10), sample(T), sample(T + 5)]
        s.add_samples(points, T, observation(T + 10), observation(T))
        s.add_samples(points + points, T, observation(T + 10), observation(T))
        rows = s.priced_segments(T, T + 10)
        self.assertEqual(s.sample_count(), 3)
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(sum(r["grid_import_kwh"] for r in rows), 1000 * 10 / 3_600_000)

    def test_backfill_clock_lag_and_unfresh_endpoints_are_unverified(self):
        cases = [
            (T + 300, T + 240, True, True),  # old raw points recovered later
            (T + 10, T - 100, True, True),  # monitor observation clock gap
            (T + 10, T, False, True),
            (T + 10, T, True, False),
        ]
        for index, (now, prior, now_fresh, prior_fresh) in enumerate(cases):
            with self.subTest(index=index):
                store = Store(Path(self.temporary.name) / (str(index) + ".sqlite3"))
                try:
                    store.add_samples([sample(T), sample(T + 5)], T,
                                      observation(now, now_fresh), observation(prior, prior_fresh))
                    self.assertEqual(store.priced_segments(T, T + 5)[0]["quality"], "unverified")
                finally:
                    store.db.close()

    def test_native_counter_jumps_do_not_become_priced_energy(self):
        instance = self.make_observer()
        instance.store.add_observation(observation(T))
        instance.store.add_observation(observation(T + 5))
        instance.store.add_samples([sample(T), sample(T + 5)], T, observation(T + 5), observation(T))
        for kind, value in (("optimise_import", 20), ("optimise_export", 65)):
            instance.store.add_quote(kind, T, T + 1800, value, T)
        _, summaries = instance.summaries(T + 5)
        overall = summaries["overall"]
        self.assertAlmostEqual(overall["grid_import_kwh"], 1000 * 5 / 3_600_000)
        self.assertAlmostEqual(overall["import_cost_gbp"], 1000 * 5 / 3_600_000 * .2)


class QuoteAndBoundaryTests(OfflineCase):
    def test_quote_validity_is_not_extended_into_next_delivery_period(self):
        s = self.make_observer().store
        s.add_samples([sample(T + 1795), sample(T + 1800), sample(T + 1805)],
                      T, observation(T + 1805), observation(T + 1795))
        s.add_quote("optimise_import", T, T + 1800, 20, T)
        rows = s.priced_segments(T, T + 3600)
        self.assertEqual(rows[0]["optimise_import_p"], 20)
        self.assertIsNone(rows[1]["optimise_import_p"])
        for start, end in ((T, T + 3600), (T + 1, T + 1801), (T, T + 1799)):
            s.add_quote("optimise_export", start, end, 65, T)
        self.assertEqual(s.db.execute("SELECT COUNT(*) FROM quotes WHERE kind='optimise_export'").fetchone()[0], 0)

    def test_forecast_and_expired_or_stale_current_quotes_are_not_delivery_quotes(self):
        instance = self.make_observer()
        now = T + 1800
        instance.state = Mock(return_value={"last_updated": iso(now), "attributes": {
            "unit_of_measurement": "p/kWh", "period": {"valid_from": iso(T),
                "valid_to": iso(now), "value": 20}, "forecast": [
                {"valid_from": iso(now), "valid_to": iso(now + 1800), "value": 999}]}})
        instance.capture_quotes(now, iso(now))
        self.assertEqual(instance.store.db.execute("SELECT COUNT(*) FROM quotes").fetchone()[0], 0)
        instance.state.return_value["attributes"]["period"] = {
            "valid_from": iso(now), "valid_to": iso(now + 1800), "value": 20}
        instance.capture_quotes(now, iso(now - 901))
        self.assertEqual(instance.store.db.execute("SELECT COUNT(*) FROM quotes").fetchone()[0], 0)

    def test_explicit_two_day_recording_bound_is_enforced(self):
        for end in (T, T - 1, T + 172801):
            with self.subTest(end=end), self.assertRaises(ValueError):
                self.make_observer(capture_until=iso(end))

    def test_raw_response_cannot_store_samples_after_capture_end(self):
        instance = self.make_observer()
        self.http.side_effect = None
        self.http.return_value = {"samples": [sample(T + 3595), sample(T + 3600), sample(T + 3605)]}
        instance.raw_samples(T + 3720, observation(T + 3720), observation(T + 3660))
        self.assertLessEqual(instance.store.last_sample(), instance.until)


class WriteGuardTests(OfflineCase):
    def test_disallowed_writes_rejected_before_credentials_and_network(self):
        instance = self.make_observer()
        with patch.object(observer.Path, "read_text", side_effect=AssertionError("Credentials forbidden")) as credentials:
            for entity in ("sensor.powerwall_3_percentage_charged", "number.powerwall_3_backup_reserve",
                           "sensor.optimise_publisher_unrecognised", "sensor.test/../../services"):
                with self.subTest(entity=entity), self.assertRaises(ValueError):
                    instance.ha(entity, {"state": 99})
            credentials.assert_not_called()
            self.http.assert_not_called()

    def test_allowed_sensor_write_uses_only_local_states_endpoint(self):
        instance = self.make_observer()
        instance.ha_token = "synthetic-token-never-read-from-disk"
        self.http.side_effect = None
        self.http.return_value = {}
        for entity in observer.ALLOWED_ENTITIES:
            instance.ha(entity, {"state": "test"})
        self.assertEqual(self.http.call_count, len(observer.ALLOWED_ENTITIES))
        for call in self.http.call_args_list:
            url, token, body = call.args
            self.assertIn(url.removeprefix("http://127.0.0.1:8123/api/states/"), observer.ALLOWED_ENTITIES)
            self.assertEqual(token, "synthetic-token-never-read-from-disk")
            self.assertEqual(body, {"state": "test"})


class SnapshotTests(OfflineCase):
    def test_native_storage_units_and_source_freshness_are_explicit(self):
        instance = self.make_observer()
        responses = {
            observer.GATEWAY + "/api/operation": {"real_mode": "autonomous", "stale": False},
            observer.GATEWAY + "/api/meters/aggregates": {
                "site": {"instant_power": -500, "energy_imported": 99999999}},
            observer.GATEWAY + "/api/system_status": {
                "nominal_full_pack_energy": 28800, "nominal_energy_remaining": 18150,
                "max_charge_power": 11040, "max_discharge_power": 11040},
            observer.GATEWAY + "/api/system_status/soe": {"percentage": 61.07, "raw_percentage": 63.02},
            "http://127.0.0.1:8677/metrics": {
                "monitor_fresh": True, "updated_at": iso(T), "connection": {
                    "fresh": True, "online": True, "data_age_seconds": 2,
                    "last_source_timestamp": T - 2}},
        }
        self.http.side_effect = lambda url: responses[url]
        result = instance.snapshot(T)
        self.assertTrue(result["source_fresh"])
        self.assertEqual(result["source_timestamp"], T - 2)
        self.assertEqual(result["inventory"]["remaining_kwh"], 18.15)
        self.assertEqual(result["inventory"]["full_pack_kwh"], 28.8)
        self.assertEqual(result["inventory"]["max_charge_kw"], 11.04)
        self.assertTrue(result["inventory"]["native_inventory_fetch_time_unverified"])
        self.assertEqual(result["counters"]["site"]["instant_power"], -500)
        responses["http://127.0.0.1:8677/metrics"]["connection"]["data_age_seconds"] = 31
        self.assertFalse(instance.snapshot(T)["source_fresh"])
        responses["http://127.0.0.1:8677/metrics"]["connection"]["data_age_seconds"] = -1
        self.assertFalse(instance.snapshot(T)["source_fresh"])


class ModelInputTests(OfflineCase):
    def model_fixture(self, instance, first_updates=None, final_updates=None,
                      reserves=(10, 10), controls=None):
        first, final = observation(T), observation(T + 3600)
        first["inventory"].update(first_updates or {})
        final["inventory"].update(final_updates or {})
        first["gateway"]["backup_reserve_percent"] = reserves[0]
        final["gateway"]["backup_reserve_percent"] = reserves[1]
        for row in (first, final):
            row.update(eon_enrolled=False, eon_fresh=True, publisher_pause=None)
        final.update(controls or {})
        instance.store.inventory_near = Mock(side_effect=[first, final])
        instance.store.observations_between = Mock(return_value=[first, final])
        segment = {"start": T, "end": T + 3600, "quality": "monitored",
                   **{key + "_kwh": 0.0 for key in POWER_FIELDS},
                   "optimise_import_p": 20, "optimise_export_p": 65,
                   "agile_import_p": 25, "agile_export_p": 15}
        segment.update(grid_import_kwh=1.0, home_kwh=1.0)
        return [segment]

    def assert_not_estimated_without_simulation(self, instance, rows):
        with patch.object(observer, "simulate_self_powered", side_effect=AssertionError("Invalid model inputs reached simulation")) as simulate:
            result = instance.model(T + 3600, rows)
        self.assertEqual(result["status"], "not_estimated")
        self.assertIsInstance(result.get("reason"), str)
        self.assertNotIn("scenarios", result)
        simulate.assert_not_called()

    def test_nonpositive_nonfinite_or_nonnumeric_capacity_and_power_decline_model(self):
        instance = self.make_observer()
        for field in ("full_pack_kwh", "max_charge_kw", "max_discharge_kw"):
            for value in (None, 0, -1, float("nan"), float("inf"), True, "27"):
                with self.subTest(field=field, value=value):
                    rows = self.model_fixture(instance, first_updates={field: value})
                    self.assert_not_estimated_without_simulation(instance, rows)

    def test_initial_and_final_remaining_must_be_finite_within_capacity(self):
        instance = self.make_observer()
        for boundary in ("initial", "final"):
            for value in (None, -0.001, 27.001, float("nan"), float("inf"), False, "10"):
                with self.subTest(boundary=boundary, value=value):
                    changes = {"remaining_kwh": value}
                    rows = self.model_fixture(instance,
                        first_updates=changes if boundary == "initial" else None,
                        final_updates=changes if boundary == "final" else None)
                    self.assert_not_estimated_without_simulation(instance, rows)

    def test_zero_and_full_remaining_inventory_are_valid_boundaries(self):
        instance = self.make_observer()
        for first, final in ((0, 0), (27, 27), (0, 27), (27, 0)):
            with self.subTest(initial=first, final=final):
                rows = self.model_fixture(instance, first_updates={"remaining_kwh": first},
                                          final_updates={"remaining_kwh": final})
                result = instance.model(T + 3600, rows)
                self.assertEqual(result["status"], "illustrative_model_only")
                self.assertEqual(len(result["scenarios"]), 6)
                self.assertTrue(all(row["result"]["eligible"] for row in result["scenarios"]))

    def test_reserve_change_or_constant_reserve_other_than_ten_declines_model(self):
        instance = self.make_observer()
        for reserves in ((10, 11), (20, 20), (0, 0), (None, None), ("10", "10")):
            with self.subTest(reserves=reserves):
                rows = self.model_fixture(instance, reserves=reserves)
                self.assert_not_estimated_without_simulation(instance, rows)

    def test_unverified_or_changed_control_ownership_declines_model(self):
        instance = self.make_observer()
        for control in ({"eon_enrolled": True}, {"eon_enrolled": None},
                        {"eon_fresh": False}, {"publisher_pause": "manual_pause"}):
            with self.subTest(control=control):
                rows = self.model_fixture(instance, controls=control)
                self.assert_not_estimated_without_simulation(instance, rows)


class FinalizationTests(OfflineCase):
    def test_synthetic_cycle_finalizes_json_csv_html_and_then_only_refreshes_health(self):
        instance = self.make_observer()
        self.wire_cycle(instance)
        for now in range(T, T + 3601, 60):
            self.cycle_at(instance, now)
        directory = Path(self.temporary.name)
        final_paths = [directory / name for name in (
            "final-report.json", "priced-segments.csv", "final-report.html")]
        before = {p.name: p.read_bytes() for p in final_paths}
        report = json.loads(before["final-report.json"])
        model = report["controller_saving_estimate"]
        self.assertEqual(model["status"], "illustrative_model_only")
        self.assertEqual(report["recording"]["model_status"], model["status"])
        self.assertEqual(len(model["scenarios"]), 6)
        self.assertTrue(all(row["result"]["eligible"] for row in model["scenarios"]))
        summary = report["recording"]["summary"]["overall"]
        self.assertEqual(summary["coverage_percent"], 100)
        self.assertEqual(summary["monitored_coverage_percent"], 100)
        self.assertAlmostEqual(summary["grid_import_kwh"], 1)
        self.assertAlmostEqual(summary["net_cost_gbp"], .2)
        self.assertIn("sampled gateway estimates", before["final-report.html"].decode())
        self.assertIn("01 January 2020", before["final-report.html"].decode())
        rows = list(csv.DictReader(io.StringIO(before["priced-segments.csv"].decode())))
        self.assertEqual(len(rows), 720)
        self.assertAlmostEqual(sum(float(row["grid_import_kwh"]) for row in rows), 1)
        self.assertEqual(instance.store.metadata("complete_report")["model_status"], "illustrative_model_only")
        counts = {table: instance.store.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                  for table in ("samples", "segments", "quotes", "price_events", "observations")}
        raw_calls = self.http.call_count
        quote_calls = instance.state.call_count
        published = instance.ha.call_count
        self.cycle_at(instance, T + 7200)
        self.assertEqual({p.name: p.read_bytes() for p in final_paths}, before)
        self.assertEqual({table: instance.store.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                          for table in counts}, counts)
        self.assertEqual(self.http.call_count, raw_calls)
        self.assertEqual(instance.state.call_count - quote_calls, 3)  # health status reads only
        self.assertGreater(instance.ha.call_count, published)
        self.assertEqual(json.loads((directory / "observer-status.json").read_text())["cycle"], "complete")
        self.assertTrue(all(call.args[0] in observer.ALLOWED_ENTITIES for call in instance.ha.call_args_list))

    def test_backlog_cannot_finalize_before_samples_reach_capture_end(self):
        instance = self.make_observer(capture_until=iso(T + 10800), windows={
            "overall": [iso(T), iso(T + 10800)],
            "overnight": [iso(T), iso(T + 1800)],
            "peak": [iso(T + 9000), iso(T + 10800)],
        })
        self.wire_cycle(instance)
        self.cycle_at(instance, T + 10800)
        self.assertIsNone(instance.store.metadata("complete_report"))
        self.assertFalse((Path(self.temporary.name) / "final-report.json").exists())
        self.assertEqual(json.loads((Path(self.temporary.name) / "summary.json").read_text())["current_phase"], "finalizing")
        for now in (T + 10860, T + 10920, T + 10980):
            self.cycle_at(instance, now)
        self.assertIsNotNone(instance.store.metadata("complete_report"))
        self.assertGreaterEqual(instance.store.last_sample(), instance.until - 5)
        self.assertLessEqual(instance.store.last_sample(), instance.until)


if __name__ == "__main__":
    unittest.main()
