"""Offline comparison contracts; no credentials, HA calls or market requests."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import collector

NOW = datetime(2026, 11, 2, 2, 15, tzinfo=timezone.utc)
POLICY = Path(__file__).resolve().parents[1] / 'customer_pricing' / 'test-policy.json'


def point(start, price, quality="forecast"):
    start = collector.instant(start)
    return {"start": collector.iso(start), "end": collector.iso(start + timedelta(minutes=30)),
            "price_p_per_kwh": price, "quality": quality}


def entity(start="2026-11-02T02:00:00Z", price=20):
    row = point(start, price)
    return {"state": str(price), "attributes": {"unit_of_measurement": "p/kWh",
            "period": {"valid_from": row["start"], "valid_to": row["end"], "value": price},
            "forecast": []}}


class RawSourceTests(unittest.TestCase):
    def test_parser_keeps_raw_quote_and_forecast_values(self):
        raw = entity()
        raw["attributes"]["forecast"] = [{"valid_from": "2026-11-02T02:30:00Z",
            "valid_to": "2026-11-02T03:00:00Z", "value": 20}]
        before = deepcopy(raw)
        observed = {}
        result = collector.parse_optimise(raw, NOW, observed)
        self.assertEqual([r["price_p_per_kwh"] for r in result], [20, 20])
        self.assertEqual([r["quality"] for r in result], ["observed", "forecast"])
        self.assertEqual(observed[result[0]["start"]]["price_p_per_kwh"], 20)
        self.assertEqual(raw, before)

    def test_align_keeps_missing_prices_and_other_sources(self):
        series = {"optimise_import": [point("2026-11-02T02:00:00Z", 20, "observed")],
                  "optimise_export": [],
                  "agile_import": [point("2026-11-02T02:00:00Z", 24)],
                  "wholesale": [point("2026-11-02T02:00:00Z", 10)]}
        before = deepcopy(series)
        rows = collector.align(series, NOW - timedelta(minutes=15), NOW + timedelta(minutes=15))
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["optimise_export"])
        self.assertEqual(rows[0]["agile_import"], 24)
        self.assertEqual(rows[0]["wholesale"], 10)
        self.assertEqual(series, before)

    def test_freshness_uses_provider_timestamp(self):
        feed = {"fetched_at": collector.iso(NOW),
                "provider_last_success": collector.iso(NOW - timedelta(seconds=901))}
        self.assertFalse(collector.source_is_fresh("optimise", feed, NOW))


class EffectiveRowsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = collector.load_customer_policy(str(POLICY))

    def row(self, start, imported=20, exported=20):
        start = collector.instant(start)
        return {"start": collector.iso(start), "end": collector.iso(start + timedelta(minutes=30)),
                "optimise_import": imported, "optimise_export": exported,
                "optimise_import_quality": "forecast", "optimise_export_quality": "forecast",
                "optimise_quality": "forecast", "agile_import": 25, "agile_export": 12,
                "wholesale": 10}

    def test_boundary_rates_follow_shared_rule(self):
        for start, imported, exported in (
            ("2026-11-01T23:30:00Z", 20, 20),
            ("2026-11-02T00:00:00Z", 17, 20),
            ("2026-11-02T05:30:00Z", 17, 20),
            ("2026-11-02T06:00:00Z", 20, 20),
            ("2026-11-02T15:30:00Z", 20, 20),
            ("2026-11-02T16:00:00Z", 20, 23),
            ("2026-11-02T18:30:00Z", 20, 23),
            ("2026-11-02T19:00:00Z", 20, 20),
        ):
            with self.subTest(start=start):
                row = collector.apply_customer_pricing([self.row(start)], self.policy)[0]
                self.assertEqual((row['optimise_import'], row['optimise_export']), (imported, exported))
                self.assertEqual((row['optimise_import_raw'], row['optimise_export_raw']), (20, 20))

    def test_london_summer_clock_and_expiry_are_preserved(self):
        for start, expected in (("2027-07-01T23:00:00Z", 17),
                                ("2027-07-02T05:00:00Z", 20),
                                ("2027-10-29T23:00:00Z", 20)):
            with self.subTest(start=start):
                row = collector.apply_customer_pricing([self.row(start)], self.policy)[0]
                self.assertEqual(row['optimise_import'], expected)

    def test_rule_can_be_reapplied_without_double_counting_or_mutation(self):
        rows = [self.row("2026-11-02T02:00:00Z")]
        before = deepcopy(rows)
        once = collector.apply_customer_pricing(rows, self.policy)
        twice = collector.apply_customer_pricing(once, self.policy)
        self.assertEqual(rows, before)
        self.assertEqual(once, twice)
        self.assertEqual(twice[0]['optimise_import'], 17)

    def test_negative_and_crossed_effective_rates_are_valid_comparisons(self):
        rows = collector.apply_customer_pricing([self.row("2026-11-02T02:00:00Z", 0, 20)], self.policy)
        self.assertEqual(rows[0]['optimise_import'], -3)
        self.assertEqual(rows[0]['optimise_export'], 20)

    def test_missing_raw_rates_stay_missing(self):
        row = collector.apply_customer_pricing([self.row("2026-11-02T02:00:00Z", None, None)], self.policy)[0]
        self.assertIsNone(row['optimise_import'])
        self.assertIsNone(row['optimise_export'])
        self.assertEqual(row['optimise_import_pricing_reason'], 'missing_base_price')

    def test_other_sources_quality_and_unconfigured_prices_are_preserved(self):
        raw = self.row("2026-11-02T02:00:00Z")
        row = collector.apply_customer_pricing([raw], self.policy)[0]
        for key in ('start', 'end', 'optimise_quality', 'optimise_import_quality',
                    'optimise_export_quality', 'agile_import', 'agile_export', 'wholesale'):
            self.assertEqual(row[key], raw[key])
        disabled = collector.apply_customer_pricing([raw], None)[0]
        self.assertEqual(disabled['optimise_import'], raw['optimise_import'])
        self.assertEqual(disabled['optimise_export'], raw['optimise_export'])


class CollectorCycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='customer-comparison-test-')
        self.addCleanup(self.directory.cleanup)
        self.network = patch.object(collector, 'get_json', side_effect=AssertionError('Network forbidden in tests'))
        self.http = self.network.start()
        self.addCleanup(self.network.stop)
        self.config = {'state_dir': self.directory.name, 'hass_url': 'http://127.0.0.1:8123',
                       'token_file': '/never-read-a-real-credential', 'customer_pricing_file': str(POLICY),
                       'optimise_import': 'sensor.test_import', 'optimise_export': 'sensor.test_export',
                       'optimise_last_success': 'sensor.test_prices_success',
                       'agile_import_product': 'fake', 'agile_export_product': 'fake'}
        self.client = collector.Collector(self.config)
        self.posts = {}
        self.gap = False

        def ha(path, body=None):
            if body is not None:
                self.posts[path.rsplit('/', 1)[-1]] = deepcopy(body)
                return body
            if path.endswith('sensor.test_prices_success'):
                return {'state': collector.iso(NOW)}
            raw = entity()
            if path.endswith('sensor.test_export'):
                raw['attributes']['period']['value'] = 20
            next_start = '2026-11-02T03:00:00Z' if self.gap else '2026-11-02T02:30:00Z'
            raw['attributes']['forecast'] = [
                {'valid_from': next_start, 'valid_to': collector.iso(collector.instant(next_start) + timedelta(minutes=30)), 'value': 20},
                {'valid_from': '2026-11-02T16:00:00Z', 'valid_to': '2026-11-02T16:30:00Z', 'value': 41 if path.endswith('sensor.test_export') else 64},
            ]
            return raw

        self.client.ha = ha
        times = ('2026-11-02T02:00:00Z', '2026-11-02T02:30:00Z', '2026-11-02T16:00:00Z')
        self.client.cache = {name: {'fetched_at': collector.iso(NOW), 'intervals': [point(ts, rate) for ts in times]}
                             for name, rate in (('agile_import', 25), ('agile_export', 12),
                                                ('wholesale', 10), ('wholesale_intraday', 10))}

    def cycle(self):
        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW if tz is not None else NOW.replace(tzinfo=None)
        with patch.object(collector, 'datetime', FixedDatetime), patch('builtins.print'):
            self.client.cycle()
        self.http.assert_not_called()

    def test_full_cycle_publishes_effective_raw_next_and_summary_without_altering_archives(self):
        self.cycle()
        prefix = 'sensor.price_comparison_'
        self.assertEqual(self.posts[prefix + 'optimise_import']['state'], 20)
        self.assertEqual(self.posts[prefix + 'optimise_import_effective']['state'], 17)
        self.assertEqual(self.posts[prefix + 'optimise_export']['state'], 20)
        self.assertEqual(self.posts[prefix + 'optimise_import_raw']['state'], 20)
        self.assertEqual(self.posts[prefix + 'optimise_import_adjustment']['state'], -3)
        self.assertEqual(self.posts[prefix + 'optimise_next_import']['state'], 17)
        self.assertEqual(self.posts[prefix + 'optimise_next_export']['state'], 20)
        self.assertEqual(self.posts[prefix + 'import_difference']['state'], -5)
        self.assertEqual(self.posts[prefix + 'import_difference_effective']['state'], -8)
        attrs = self.posts['sensor.optimise_agile_comparison']['attributes']
        self.assertEqual(attrs['price_basis'], 'customer_effective')
        self.assertEqual(attrs['customer_pricing']['rule_id'], 'test-earlybird-v1')
        self.assertEqual(attrs['sources']['optimise']['last_success'], collector.iso(NOW))
        self.assertNotEqual(attrs['summary']['export']['optimise_average'], attrs['raw_summary']['export']['optimise_average'])
        for direction in ('import', 'export'):
            saved = json.loads((Path(self.directory.name) / 'observed.json').read_text())[direction]
            self.assertEqual(next(iter(saved.values()))['price_p_per_kwh'], 20)
            self.assertNotIn('optimise_import_raw', next(iter(saved.values())))
        scalar = self.posts[prefix + 'optimise_next_import']['attributes']
        self.assertEqual(scalar['valid_from'], '2026-11-02T02:30:00Z')
        self.assertEqual(scalar['customer_pricing_end_date_exclusive'], '2027-10-30')
        self.assertEqual(scalar['provider_last_success'], collector.iso(NOW))
        self.cycle()
        self.assertEqual(self.posts[prefix + 'optimise_import']['state'], 20)
        self.assertEqual(self.posts[prefix + 'optimise_import_effective']['state'], 17)
        self.assertEqual(self.posts[prefix + 'optimise_import_raw']['state'], 20)

    def test_next_price_gap_is_unavailable(self):
        self.gap = True
        self.cycle()
        self.assertEqual(self.posts['sensor.price_comparison_optimise_next_import']['state'], 'unavailable')
        self.assertEqual(self.posts['sensor.price_comparison_optimise_next_export']['state'], 'unavailable')

    def test_notes_describe_confirmed_customer_rule(self):
        notes = ' '.join(collector.customer_pricing_notes(self.client.customer_policy))
        for text in ('3p/kWh', '00:00–06:00', '16:00–19:00', 'Europe/London', '2026-10-30', '2027-10-30'):
            self.assertIn(text, notes)
        self.assertIn('no customer-specific incentive', ' '.join(collector.customer_pricing_notes(None)))


if __name__ == "__main__":
    unittest.main()
