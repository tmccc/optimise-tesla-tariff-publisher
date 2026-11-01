"""Explicit Tessie GET-only tariff backup for read-only preparation."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
from urllib import request

from active_mode import read_active_mode
from auth import read_secret_file
from tariff_schema import canonical_hash, validate_tariff

MAX_RESPONSE = 2 * 1024 * 1024


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('redirect_refused')


def _get(path):
    if path not in ('auth/tesla_scopes', 'api/1/products') and not re.fullmatch(
        r'api/1/energy_sites/[1-9][0-9]*/(?:site_info|tariff_rate)',path):
        raise ValueError('tessie_read_endpoint_denied')
    token = read_secret_file('TESSIE_TOKEN_FILE')
    req = request.Request('https://api.tessie.com/' + path, method='GET',
        headers={'Authorization': 'Bearer ' + token})
    opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(req, timeout=20) as response:
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ValueError()
        value=json.loads(raw)
        if not isinstance(value,dict):raise ValueError()
        return value
    except Exception:
        raise ValueError('tessie_read_failed_private_errors_suppressed') from None


def _safe_tariff(value, depth=0):
    sensitive = {'token', 'access_token', 'refresh_token', 'password', 'secret',
        'credentials', 'authorization', 'cookie', 'cookies', 'headers', 'client_secret'}
    if depth > 16:
        raise ValueError('tariff_structure_invalid')
    if isinstance(value, dict):
        if len(value) > 1024:
            raise ValueError('tariff_structure_invalid')
        for key, child in value.items():
            if not isinstance(key, str) or key.lower() in sensitive:
                raise ValueError('sensitive_tariff_structure_rejected')
            _safe_tariff(child, depth + 1)
    elif isinstance(value, list):
        if len(value) > 4096:
            raise ValueError('tariff_structure_invalid')
        for child in value:
            _safe_tariff(child, depth + 1)


def read_live_readback():
    """Return (private backup, sanitized readback); never send a Tesla command."""
    scopes = _get('auth/tesla_scopes').get('scopes')
    if not isinstance(scopes, list) or any(not isinstance(item, str) for item in scopes):
        raise ValueError('tessie_scope_response_invalid')
    products = _get('api/1/products').get('response')
    if not isinstance(products, list):
        raise ValueError('tessie_product_response_invalid')
    sites = [row for row in products if isinstance(row, dict)
        and type(row.get('energy_site_id')) is int
        and isinstance(row.get('components'), dict)
        and row['components'].get('battery') is True]
    configured = os.environ.get('TESLA_ENERGY_SITE_ID')
    if configured:
        try:
            selected = int(configured)
        except ValueError:
            raise ValueError('configured_site_invalid') from None
        sites = [row for row in sites if row['energy_site_id'] == selected]
    if len(sites) != 1 or sites[0]['energy_site_id'] <= 0:
        raise ValueError('explicit_battery_site_selection_required')
    site = sites[0]['energy_site_id']
    prefix = 'api/1/energy_sites/' + str(site) + '/'
    info = _get(prefix + 'site_info').get('response')
    tariff = _get(prefix + 'tariff_rate').get('response')
    if not isinstance(info, dict) or not isinstance(tariff, dict):
        raise ValueError('tessie_site_readback_invalid')
    _safe_tariff(tariff)
    if validate_tariff(tariff, strict_compatibility=False):
        raise ValueError('tessie_tariff_schema_invalid')
    active = read_active_mode()
    captured = datetime.now(timezone.utc).isoformat()
    digest = canonical_hash(tariff)
    backup = {'captured_at': captured, 'energy_site_id': site,
        'source': 'Explicit Tessie HTTPS GET; no Tesla controls',
        'tariff_rate_response': tariff, 'sha256': digest}
    settings = {}
    if info.get('default_real_mode') in ('autonomous', 'self_consumption', 'backup'):
        settings['default_real_mode'] = info['default_real_mode']
    reserve = info.get('backup_reserve_percent')
    if type(reserve) in (int, float) and 0 <= reserve <= 100:
        settings['backup_reserve_percent'] = reserve
    readback = {'captured_at': captured, 'tariff_sha256': digest,
        'energy_data_scope': 'energy_device_data' in scopes,
        'energy_commands_scope': 'energy_cmds' in scopes,
        'site_settings': settings, 'active_mode': active['mode'],
        'active_mode_source': active['mode_source'], 'control_requests': 0}
    return backup, readback
