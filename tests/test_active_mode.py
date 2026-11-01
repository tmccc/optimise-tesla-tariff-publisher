from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch

import active_mode
from active_mode import ActiveModeError, read_active_mode


NOW = datetime(2026, 11, 1, 14, 0, tzinfo=timezone.utc)
GOOD = {"real_mode": "self_consumption", "backup_reserve_percent": 10.0,
        "grid_charging": True, "grid_export": "battery_ok", "stale": False,
        "last_updated": None}


class ActiveModeTests(unittest.TestCase):
    def read(self, data=None, status=200):
        body = json.dumps(GOOD if data is None else data).encode()
        return read_active_mode(transport=lambda: (status, body), now=NOW)

    def test_good_is_hardware_and_observation_not_device_poll_time(self):
        with patch.object(active_mode.socket, "socket", side_effect=AssertionError("network forbidden")):
            result = self.read()
        self.assertEqual(result["mode"], "self_consumption")
        self.assertEqual(result["mode_source"], "local_gateway_TEDAPI")
        self.assertEqual(result["mode_observed_at"], NOW.isoformat())
        self.assertTrue(result["cloud_default_mode_ignored"])
        self.assertFalse(result["gateway_reported_stale"])
        self.assertFalse(result["gateway_poll_timestamp_available"])
        self.assertEqual(result["hardware_preserve_settings"], {
            "backup_reserve": 10.0, "grid_charging": True, "export_permission": "battery_ok"})

    def test_even_falsey_injected_transport_never_uses_network(self):
        class Fake:
            def __bool__(self):
                return False

            def __call__(self):
                return 200, json.dumps(GOOD).encode()

        with patch.object(active_mode.socket, "socket", side_effect=AssertionError("network forbidden")):
            self.assertEqual(read_active_mode(transport=Fake(), now=NOW)["mode"], "self_consumption")

    def test_only_three_canonical_modes(self):
        for mode in ("autonomous", "self_consumption", "backup"):
            self.assertEqual(self.read({**GOOD, "real_mode": mode})["mode"], mode)
        for mode in ("auto", "Self-Powered", "", None, 1, [], {}):
            with self.subTest(mode=mode), self.assertRaises(ActiveModeError):
                self.read({**GOOD, "real_mode": mode})

    def test_stale_must_be_exact_false(self):
        for value in (True, None, 0, "false", [], {}):
            with self.subTest(stale=value), self.assertRaises(ActiveModeError):
                self.read({**GOOD, "stale": value})
        missing = deepcopy(GOOD)
        del missing["stale"]
        with self.assertRaises(ActiveModeError):
            self.read(missing)

    def test_reserve_is_finite_numeric_with_limits(self):
        for reserve in (0, 100, 10.5):
            self.assertEqual(self.read({**GOOD, "backup_reserve_percent": reserve})[
                "hardware_preserve_settings"]["backup_reserve"], reserve)
        for reserve in (-0.1, 100.1, True, None, "10", float("nan"), float("inf")):
            with self.subTest(reserve=reserve), self.assertRaises(ActiveModeError):
                self.read({**GOOD, "backup_reserve_percent": reserve})

    def test_grid_optional_unknown_never_fabricated(self):
        for grid in (True, False):
            self.assertIs(self.read({**GOOD, "grid_charging": grid})[
                "hardware_preserve_settings"]["grid_charging"], grid)
        for data in ({k: v for k, v in GOOD.items() if k != "grid_charging"},
                     {**GOOD, "grid_charging": None}):
            self.assertEqual(self.read(data)["hardware_preserve_settings"]["grid_charging"], "not_reported")
        for grid in (0, 1, "true", "not_reported", [], {}):
            with self.subTest(grid=grid), self.assertRaises(ActiveModeError):
                self.read({**GOOD, "grid_charging": grid})

    def test_export_is_canonical(self):
        for export in ("battery_ok", "pv_only", "never"):
            self.assertEqual(self.read({**GOOD, "grid_export": export})[
                "hardware_preserve_settings"]["export_permission"], export)
        for export in (None, "everything", True, "", [], {}):
            with self.subTest(export=export), self.assertRaises(ActiveModeError):
                self.read({**GOOD, "grid_export": export})

    def test_last_updated_is_not_treated_as_local_poll_timestamp(self):
        result = self.read({**GOOD, "last_updated": NOW.isoformat()})
        self.assertEqual(result["gateway_reported_last_updated"], NOW.isoformat())
        self.assertFalse(result["gateway_poll_timestamp_available"])
        for value in ("invalid", "2026-11-01T14:00:00", 17, {}):
            with self.subTest(value=value), self.assertRaises(ActiveModeError):
                self.read({**GOOD, "last_updated": value})

    def test_redirect_and_all_non_success_statuses_rejected(self):
        for status in (301, 302, 303, 307, 308, 401, 403, 500, 503, True, "200"):
            with self.subTest(status=status), self.assertRaises(ActiveModeError):
                self.read(status=status)

    def test_malformed_and_oversized_body(self):
        for body in (b"", b"no json", b"[]", b"null", b"0", b"{}",
                     b"{\"stale\":false,\"stale\":false}",
                     b" " * (active_mode.MAX_BODY_BYTES + 1), "secret"):
            with self.subTest(body_type=type(body)), self.assertRaises(ActiveModeError):
                read_active_mode(transport=lambda: (200, body), now=NOW)

    def test_cloud_default_cannot_override_hardware(self):
        data = {**GOOD, "default_real_mode": "autonomous", "private_unknown_field": "discard me"}
        result = self.read(data)
        self.assertEqual(result["mode"], "self_consumption")
        self.assertNotIn("discard me", repr(result))

    def test_errors_discard_private_messages(self):
        def failed():
            raise RuntimeError("private-server-payload")
        with self.assertRaises(ActiveModeError) as caught:
            read_active_mode(transport=failed, now=NOW)
        self.assertEqual(str(caught.exception), "active_mode_unavailable")
        self.assertTrue(caught.exception.__suppress_context__)

    def test_observation_clock_must_be_aware_and_is_utc(self):
        with self.assertRaises(ActiveModeError):
            read_active_mode(transport=lambda: (200, json.dumps(GOOD).encode()),
                             now=datetime(2026, 11, 1))
        result = read_active_mode(transport=lambda: (200, json.dumps(GOOD).encode()),
            now=NOW.astimezone(timezone(timedelta(hours=2))))
        self.assertEqual(result["mode_observed_at"], NOW.isoformat())

    def test_transport_has_fixed_loopback_get_no_credentials(self):
        self.assertEqual(active_mode.ENDPOINT, "http://127.0.0.1:8675/api/operation")
        self.assertTrue(active_mode._REQUEST.startswith(b"GET /api/operation HTTP/1.1\r\n"))
        self.assertNotIn(b"Authorization", active_mode._REQUEST)
        self.assertNotIn(b"Cookie", active_mode._REQUEST)
        self.assertIn(b"Connection: close", active_mode._REQUEST)
        self.assertLessEqual(active_mode.TIMEOUT_SECONDS, 5)
        self.assertLessEqual(active_mode.MAX_RESPONSE_BYTES, 32768)

    def test_one_total_deadline(self):
        with patch.object(active_mode.time, "monotonic", return_value=20):
            with self.assertRaises(TimeoutError):
                active_mode._remaining(19)
            self.assertEqual(active_mode._remaining(22), 2)


if __name__ == "__main__":
    unittest.main()
