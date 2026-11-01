"""Read the local gateway's polled operating mode, never cloud defaults.

Only GET /api/operation on the fixed IPv4 loopback server is implemented.
The server returns its background-poll cache: the HTTP observation time below
does not assert the age of that device poll. No authentication is required or
loaded, and the transport ignores proxy configuration and never redirects.
"""
from __future__ import annotations

from datetime import datetime, timezone
import http.client
import io
import json
import math
import select
import socket
import time


ENDPOINT = "http://127.0.0.1:8675/api/operation"
TIMEOUT_SECONDS = 5.0
MAX_BODY_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 32 * 1024
MODES = frozenset({"autonomous", "self_consumption", "backup"})
EXPORT_RULES = frozenset({"battery_ok", "pv_only", "never"})
_REQUEST = (b"GET /api/operation HTTP/1.1\r\nHost: 127.0.0.1:8675\r\n"
            b"Accept: application/json\r\nConnection: close\r\n\r\n")


class ActiveModeError(ValueError):
    """Fail-closed error with no server content or private exception attached."""


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return remaining


class _ResponseSocket:
    def __init__(self, raw):
        self.raw = raw

    def makefile(self, *args, **kwargs):
        return io.BytesIO(self.raw)


def _read_loopback_operation():
    """Return (status, body) under a total wall-time and response-size bound.

    A literal IPv4 socket prevents proxies, DNS, TLS configuration, redirects
    or an injected environment variable from changing the destination. Reading
    a Connection: close response under one monotonic deadline also bounds a
    server which trickles bytes, unlike an individual socket read timeout.
    """
    deadline = time.monotonic() + TIMEOUT_SECONDS
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(_remaining(deadline))
        connection.connect(("127.0.0.1", 8675))
        connection.setblocking(False)
        sent = 0
        while sent < len(_REQUEST):
            if not select.select([], [connection], [], _remaining(deadline))[1]:
                raise TimeoutError
            try:
                count = connection.send(_REQUEST[sent:])
            except BlockingIOError:
                continue
            if count <= 0:
                raise ConnectionError
            sent += count
        pieces = []
        size = 0
        while True:
            if not select.select([connection], [], [], _remaining(deadline))[0]:
                raise TimeoutError
            try:
                piece = connection.recv(min(4096, MAX_RESPONSE_BYTES + 1 - size))
            except BlockingIOError:
                continue
            if not piece:
                break
            pieces.append(piece)
            size += len(piece)
            if size > MAX_RESPONSE_BYTES:
                raise ValueError
    # Parsing only occurs after the bounded socket read has finished.
    response = http.client.HTTPResponse(_ResponseSocket(b"".join(pieces)))
    try:
        response.begin()
        if response.status != 200:
            return response.status, b""
        body = response.read(MAX_BODY_BYTES + 1)
        if len(body) > MAX_BODY_BYTES:
            raise ValueError
        return response.status, body
    finally:
        response.close()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _bad_constant(_):
    raise ValueError


def _aware_time(value):
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError
    return value.astimezone(timezone.utc)


def read_active_mode(*, transport=None, now=None):
    """Return a small validated hardware snapshot or raise ActiveModeError.

    ``transport`` is an optional zero-argument callable returning (HTTP status,
    body bytes), making tests independent of both networking and credentials.
    Unknown/missing optional grid charging is explicit ``not_reported``;
    malformed values are rejected. Reserve, export rule, mode and stale=false
    are required. No cloud mode field is consumed.
    """
    try:
        fetch = _read_loopback_operation if transport is None else transport
        status, body = fetch()
        if type(status) is not int or status != 200:
            raise ValueError
        if type(body) is not bytes or not 1 <= len(body) <= MAX_BODY_BYTES:
            raise ValueError
        data = json.loads(body, object_pairs_hook=_object, parse_constant=_bad_constant)
        if type(data) is not dict or data.get("stale") is not False:
            raise ValueError
        mode = data.get("real_mode")
        reserve = data.get("backup_reserve_percent")
        grid = data.get("grid_charging")
        export = data.get("grid_export")
        if type(mode) is not str or mode not in MODES:
            raise ValueError
        if type(reserve) not in {int, float} or not math.isfinite(reserve) or not 0 <= reserve <= 100:
            raise ValueError
        if grid is not None and type(grid) is not bool:
            raise ValueError
        if type(export) is not str or export not in EXPORT_RULES:
            raise ValueError
        reported_time = data.get("last_updated")
        if reported_time is not None:
            if type(reported_time) is not str:
                raise ValueError
            reported_time = _aware_time(datetime.fromisoformat(reported_time)).isoformat()
        observed = _aware_time(now if now is not None else datetime.now(timezone.utc))
        return {
            "mode": mode,
            "mode_source": "local_gateway_TEDAPI",
            "mode_observed_at": observed.isoformat(),
            "cloud_default_mode_ignored": True,
            "hardware_preserve_settings": {
                "backup_reserve": reserve,
                "grid_charging": grid if grid is not None else "not_reported",
                "export_permission": export,
            },
            "gateway_reported_stale": False,
            # legacy.py uses last_updated only for a hybrid stale fallback.
            # It is not a timestamp for the local device's polling cycle.
            "gateway_reported_last_updated": reported_time,
            "gateway_poll_timestamp_available": False,
        }
    except Exception:
        raise ActiveModeError("active_mode_unavailable") from None
