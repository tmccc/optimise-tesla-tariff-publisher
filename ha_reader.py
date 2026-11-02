"""Explicit Home Assistant state reads; no session discovery or auth-store access.

HASS_TOKEN_FILE must name a private token file. State entity IDs are configurable
through the HASS_*_ENTITY variables below. No network or file IO runs on import.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
import re
from urllib import parse, request

from auth import read_secret_file

UTC = timezone.utc
MAX_RESPONSE = 1024 * 1024
ENTITY_DEFAULTS = {
    'import': ('HASS_IMPORT_PRICE_ENTITY', 'sensor.optimise_import_price'),
    'export': ('HASS_EXPORT_PRICE_ENTITY', 'sensor.optimise_export_price'),
    'prices_last_success': ('HASS_PRICES_LAST_SUCCESS_ENTITY', 'sensor.optimise_prices_last_successful_update'),
    'battery_enrolled': ('HASS_BATTERY_ENROLLED_ENTITY', 'binary_sensor.optimise_battery_enrolled'),
    'status_last_success': ('HASS_STATUS_LAST_SUCCESS_ENTITY', 'sensor.optimise_status_last_successful_update'),
}


def _instant(value):
    if not isinstance(value, str) or len(value) > 80:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            return None
        return stamp.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def _iso(value):
    stamp = _instant(value)
    return stamp.isoformat() if stamp is not None else None


def _entity(key):
    variable, default = ENTITY_DEFAULTS[key]
    entity = os.environ.get(variable, default)
    domain = 'binary_sensor' if key == 'battery_enrolled' else 'sensor'
    if not isinstance(entity, str) or not re.fullmatch(domain + r'\.[a-z0-9_]{1,200}', entity):
        raise ValueError('configured_entity_invalid')
    return entity


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('redirect_refused')


def _base_url():
    value = os.environ.get('HASS_URL', 'http://127.0.0.1:8123')
    try:
        parts = parse.urlsplit(value)
        if (parts.username is not None or parts.password is not None or parts.query
            or parts.fragment or parts.path not in ('', '/') or not parts.hostname
            or parts.scheme not in ('http', 'https')):
            raise ValueError()
        if parts.scheme == 'http' and parts.hostname not in ('127.0.0.1', 'localhost', '::1'):
            raise ValueError()
        if parts.port is not None and not 1 <= parts.port <= 65535:
            raise ValueError()
        return value.rstrip('/')
    except (TypeError, ValueError):
        raise ValueError('home_assistant_url_invalid') from None


def _request(path, *, refresh_entity=None):
    allowed = path.startswith('/api/states/') or path == '/api/services/homeassistant/update_entity'
    if not allowed:
        raise ValueError('home_assistant_endpoint_denied')
    token = read_secret_file('HASS_TOKEN_FILE')
    headers = {'Authorization': 'Bearer ' + token}
    data = None
    if refresh_entity is not None:
        if path != '/api/services/homeassistant/update_entity' or refresh_entity != _entity('battery_enrolled'):
            raise ValueError('telemetry_refresh_target_denied')
        headers['Content-Type'] = 'application/json'
        data = json.dumps({'entity_id': [refresh_entity]}).encode()
    req = request.Request(_base_url() + path, data=data,
        method='POST' if data is not None else 'GET', headers=headers)
    opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(req, timeout=60 if data is not None else 10) as response:
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ValueError()
        return json.loads(raw)
    except Exception:
        raise ValueError('home_assistant_request_failed_private_errors_suppressed') from None


def _state(key):
    entity = _entity(key)
    value = _request('/api/states/' + entity)
    if not isinstance(value, dict) or value.get('entity_id') != entity:
        raise ValueError('home_assistant_state_identity_invalid')
    return value


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) and abs(value) <= 1_000_000_000 else None
    except OverflowError:
        return None


def _period(value):
    if not isinstance(value, dict):
        return {'invalid_period_type': True}
    row = {'valid_from': _iso(value.get('valid_from')), 'valid_to': _iso(value.get('valid_to')),
        'value': _number(value.get('value'))}
    for field in ('quality', 'interval_basis'):
        allowed = {'history', 'current', 'forecast'} if field == 'quality' else {'provider_end', 'provisional_30m'}
        row[field] = value.get(field) if value.get(field) in allowed else None
    row['estimate'] = value.get('estimate') if isinstance(value.get('estimate'), bool) else None
    return row


def read_live_source():
    """Read three configured price/freshness states; never read history or auth stores."""
    channels = {}
    errors = []
    for direction in ('import', 'export'):
        state = _state(direction)
        attrs = state.get('attributes') if isinstance(state.get('attributes'), dict) else {}
        if (attrs.get('unit_of_measurement') != 'p/kWh' or attrs.get('currency') != 'GBP'
            or attrs.get('price_basis') != ('import_cost' if direction == 'import' else 'export_earnings')
            or state.get('state') in ('unavailable', 'unknown')):
            errors.append(direction + '_metadata_or_availability_invalid')
        if (attrs.get('customer_adjustment_applied') is True
            or attrs.get('customer_pricing_rule') or attrs.get('customer_pricing')):
            errors.append(direction + '_customer_adjusted_source_rejected')
        try:
            numeric_state=float(state.get('state'))
            if not math.isfinite(numeric_state):raise ValueError()
        except (ValueError,TypeError,OverflowError):
            errors.append(direction + '_state_value_invalid')
        forecast = attrs.get('forecast')
        if not isinstance(forecast, list) or len(forecast) > 100:
            raise ValueError('home_assistant_forecast_invalid')
        period = attrs.get('period')
        channels[direction] = ([_period(period)] if period is not None else []) + [_period(row) for row in forecast]
    last_success = _iso(_state('prices_last_success').get('state'))
    captured = datetime.now(UTC)
    return {'schema_version': 1, 'captured_at': captured.isoformat(),
        'last_success': last_success, 'timezone': 'Europe/London', 'unit': 'p/kWh', 'currency': 'GBP',
        'price_basis':'raw_eon_optimise_api',
        'source': 'Explicit Home Assistant bearer-token state API; no history or auth-store reads',
        'channels': channels, 'source_validation_errors': errors}


def read_battery_status(*, refresh=False):
    """Read enrollment and its success timestamp; refresh only by explicit opt-in."""
    requested = None
    if refresh:
        if os.environ.get('HASS_ALLOW_STATUS_REFRESH') != '1':
            raise ValueError('explicit_telemetry_refresh_permission_required')
        requested = datetime.now(UTC)
        _request('/api/services/homeassistant/update_entity', refresh_entity=_entity('battery_enrolled'))
    enrolled = _state('battery_enrolled').get('state')
    stamp = _instant(_state('status_last_success').get('state'))
    now = datetime.now(UTC)
    fresh = bool(stamp is not None and 0 <= (now - stamp).total_seconds() < 45 * 60)
    if requested is not None:
        fresh = bool(fresh and stamp.timestamp() >= requested.timestamp() - 1
            and 0 <= (now - stamp).total_seconds() < 30)
    known = enrolled in ('on', 'off') and fresh
    return {'captured_at': now.isoformat(), 'telemetry_refresh_requested': refresh,
        'enrollment_refresh_requested_at': requested.isoformat() if requested else None,
        'states': [
            {'key': 'battery_enrolled', 'state': enrolled if known else 'unavailable'},
            {'key': 'status_last_update', 'state': stamp.isoformat() if stamp is not None else None},
        ],
        'diagnostics': [{'status': {'fresh': fresh, 'last_update_success': known,
            'provider_error': None}}],
        'source': 'Configured HA enrollment and status-success entities; provider diagnostics not independently queried',
        'control_requests': 0}


def read_enrollment(*, refresh=False):
    status = read_battery_status(refresh=refresh)
    rows = {row['key']: row['state'] for row in status['states']}
    known = status['diagnostics'][0]['status']['last_update_success']
    return {'battery_enrolled': {'on': True, 'off': False}.get(rows['battery_enrolled']) if known else None,
        'enrolled_last_success': rows['status_last_update'] if known else None}
