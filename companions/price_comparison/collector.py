"""Read-only price comparison feeds; publishes only its own HA sensor states.

Never calls HA services, supplier sign-in, Tesla, or any battery controls.
Observed Optimise quotes are archived separately from revisable forecasts.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from wholesale_feeds import fetch_neso_n2ex, fetch_elexon_mid

UTC = timezone.utc
HALF_HOUR = timedelta(minutes=30)
OCTOPUS = 'https://api.octopus.energy/v1/'
PREFIX = 'sensor.price_comparison_'
POLL_SECONDS = {'optimise': 0, 'agile_import': 900, 'agile_export': 900,
                'wholesale': 1800, 'wholesale_intraday': 900}
FRESH_SECONDS = {'optimise': 900, 'agile_import': 3600, 'agile_export': 3600,
                 'wholesale': 5400, 'wholesale_intraday': 3600}


def load_customer_policy(path):
    """Load the optional shared, account-specific rule without changing raw feeds."""
    if path is None:
        return None
    shared = str(Path.home() / '.local/lib/optimise-customer-pricing')
    if shared not in sys.path:
        sys.path.insert(0, shared)
    from customer_pricing import load_policy
    return load_policy(path)


def instant(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Timestamp requires an offset')
    return result.astimezone(UTC)


def iso(value):
    return value.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def load(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def save(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as output:
        os.chmod(temporary, 0o600)
        json.dump(value, output, separators=(',', ':'), allow_nan=False)
    temporary.replace(path)


def get_json(url, headers=None, timeout=20, body=None):
    request = Request(url, headers=headers or {}, data=None if body is None else json.dumps(body, allow_nan=False).encode())
    with urlopen(request, timeout=timeout) as response:
        raw = response.read(8_000_001)
    if len(raw) > 8_000_000:
        raise ValueError('JSON response exceeds limit')
    return json.loads(raw)


def valid_interval(start, end):
    return start.minute in (0, 30) and not start.second and not start.microsecond and end > start and end - start <= timedelta(hours=1)


def parse_octopus(records):
    result = {}
    for row in records:
        price = number(row.get('value_inc_vat'))
        if price is None or not row.get('valid_from') or not row.get('valid_to'):
            continue
        start, end = instant(row['valid_from']), instant(row['valid_to'])
        if not valid_interval(start, end):
            raise ValueError('Unexpected Agile interval')
        key = iso(start)
        point = {'start': key, 'end': iso(end), 'price_p_per_kwh': price}
        if key in result and result[key] != point:
            raise ValueError('Conflicting Agile prices')
        result[key] = point
    return sorted(result.values(), key=lambda row: row['start'])


def fetch_octopus(product, now):
    url = OCTOPUS + f'products/{product}/electricity-tariffs/E-1R-{product}-G/standard-unit-rates/?' + urlencode({
        'period_from': iso(now - timedelta(days=2)), 'period_to': iso(now + timedelta(days=2)), 'page_size': 500,
    })
    response = get_json(url)
    if response.get('next'):
        raise ValueError('Unexpected Agile pagination')
    rows = parse_octopus(response['results'])
    if not rows:
        raise ValueError('Empty Agile feed')
    return {'fetched_at': iso(now), 'url': url, 'product': product, 'intervals': rows}


def parse_optimise(entity, now, observed=None):
    attrs = entity.get('attributes', {})
    if attrs.get('unit_of_measurement') != 'p/kWh':
        raise ValueError('Unexpected Optimise units')
    result = []
    for index, point in enumerate([attrs.get('period', {})] + attrs.get('forecast', [])):
        price = number(point.get('value'))
        if price is None or not point.get('valid_from') or not point.get('valid_to'):
            continue
        start, end = instant(point['valid_from']), instant(point['valid_to'])
        if end - start != HALF_HOUR or not valid_interval(start, end):
            raise ValueError('Unexpected Optimise interval')
        current = index == 0 and start <= now < end
        if not current and start < now:
            continue
        row = {'start': iso(start), 'end': iso(end), 'price_p_per_kwh': price,
               'quality': 'observed' if current else 'forecast'}
        result.append(row)
        if current and observed is not None:
            observed[row['start']] = row
    return result


def align(series, start, end):
    """Preserve missing periods; only expand explicitly covering hourly quotes."""
    result = []
    cursor = start.replace(minute=30 * (start.minute // 30), second=0, microsecond=0)
    while cursor < end:
        row = {'start': iso(cursor), 'end': iso(cursor + HALF_HOUR), 'optimise_quality': None}
        for name, intervals in series.items():
            matches = [point for point in intervals if instant(point['start']) <= cursor and instant(point['end']) >= cursor + HALF_HOUR]
            if len(matches) > 1:
                raise ValueError('Overlapping price intervals')
            row[name] = matches[0]['price_p_per_kwh'] if matches else None
            if matches and name.startswith('optimise_'):
                row[name + '_quality'] = matches[0].get('quality')
        qualities = {row[key + '_quality'] for key in ('optimise_import', 'optimise_export') if key + '_quality' in row}
        row['optimise_quality'] = next(iter(qualities)) if len(qualities) == 1 else None
        if any(row.get(key) is not None for key in series):
            result.append(row)
        cursor += HALF_HOUR
    return result


def paired_summary(rows, now, hours=24):
    selected = [row for row in rows if now <= instant(row['start']) < now + timedelta(hours=hours)]
    result = {'basis': 'Equal energy in each matched half-hour; excludes standing charges, consumption and battery scheduling', 'hours_requested': hours}
    for channel in ('import', 'export'):
        pairs = [(r['optimise_' + channel], r['agile_' + channel]) for r in selected if r.get('optimise_' + channel) is not None and r.get('agile_' + channel) is not None]
        result[channel] = {
            'matched_periods': len(pairs), 'matched_hours': len(pairs) / 2,
            'optimise_average': round(sum(a for a, _ in pairs) / len(pairs), 4) if pairs else None,
            'agile_average': round(sum(b for _, b in pairs) / len(pairs), 4) if pairs else None,
            'optimise_minus_agile_average': round(sum(a - b for a, b in pairs) / len(pairs), 4) if pairs else None,
        }
    return result


def apply_customer_pricing(rows, policy):
    """Return effective display rows while retaining every original API value.

    Alignment, quote quality and source timestamps stay unchanged. Reapplying a
    rule uses the preserved raw value, so an already decorated row cannot receive
    the same incentive twice. The cache and observed archive never enter here.
    """
    result = []
    for source in rows:
        row = dict(source)
        for channel in ('import', 'export'):
            field = 'optimise_' + channel
            raw = source[field + '_raw'] if field + '_raw' in source else source.get(field)
            if policy is None:
                decision = {'raw_p_per_kwh': raw, 'effective_p_per_kwh': raw,
                            'adjustment_p_per_kwh': 0 if raw is not None else None,
                            'applied': False, 'reason': 'no_customer_rule', 'rule_id': None}
            else:
                decision = policy.adjust_price(raw, channel, row['start'], row['end'])
            row[field + '_raw'] = decision['raw_p_per_kwh']
            row[field] = decision['effective_p_per_kwh']
            row[field + '_adjustment'] = decision['adjustment_p_per_kwh']
            row[field + '_incentive_applied'] = decision['applied']
            row[field + '_pricing_reason'] = decision['reason']
            row[field + '_pricing_rule'] = decision['rule_id']
        result.append(row)
    return result


def customer_pricing_notes(policy):
    if policy is None:
        return ['Optimise rates use the original E.ON API values; no customer-specific incentive is configured.']
    metadata = policy.metadata()
    imports, exports = metadata['import_window'], metadata['export_window']
    return [
        f"Optimise comparison rates include your {metadata['import_discount_p']:g}p/kWh import discount {imports['start']}–{imports['end']} and {metadata['export_bonus_p']:g}p/kWh export bonus {exports['start']}–{exports['end']} in {metadata['timezone']} local time.",
        f"Your offer runs from {metadata['start_date']} to {metadata['end_date_exclusive']} exclusive; rates outside that period use the original API price.",
        'Original E.ON API rates are retained separately. Incentives are applied once after interval alignment; Agile and wholesale prices are unchanged.',
    ]


def source_is_fresh(name, feed, now):
    stamp = feed.get('provider_last_success') if name == 'optimise' else feed.get('fetched_at')
    if not stamp:
        return False
    try:
        age = (now - instant(stamp)).total_seconds()
    except (TypeError, ValueError):
        return False
    return -60 <= age <= FRESH_SECONDS[name]


class Collector:
    def __init__(self, config):
        self.config = config
        self.directory = Path(config['state_dir'])
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.cache = load(self.directory / 'cache.json', {})
        self.observed = load(self.directory / 'observed.json', {'import': {}, 'export': {}})
        self.base = config['hass_url'].rstrip('/')
        if self.base != 'http://127.0.0.1:8123':
            raise ValueError('Only local Home Assistant is allowed')
        self.customer_policy = load_customer_policy(config.get('customer_pricing_file'))

    def ha(self, path, body=None):
        if body is not None and not (re.fullmatch(r'/api/states/sensor\.price_comparison_[a-z0-9_]+', path) or path == '/api/states/sensor.optimise_agile_comparison'):
            raise ValueError('Only comparison sensor writes are allowed')
        token = Path(self.config['token_file']).read_text().strip()
        return get_json(self.base + path, {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, body=body)

    def optimise(self, now):
        last = self.ha('/api/states/' + self.config['optimise_last_success'])
        stamp = instant(last['state'])
        if not -60 <= (now - stamp).total_seconds() <= 900:
            raise ValueError('Optimise prices are stale')
        data = {}
        for channel in ('import', 'export'):
            state = self.ha('/api/states/' + self.config['optimise_' + channel])
            if state['state'] in ('unknown', 'unavailable'):
                raise ValueError('Optimise price is unavailable')
            data[channel] = parse_optimise(state, now, self.observed[channel])
        if not data['import'] or not data['export']:
            raise ValueError('Optimise prices are empty')
        return {'fetched_at': iso(now), 'provider_last_success': iso(stamp), **data}

    def seed_history(self, now):
        """One bounded request for two entities, retaining only explicit current quotes."""
        if (self.directory / 'history-seeded.json').exists():
            return
        start = iso(now - timedelta(hours=24))
        query = urlencode({'filter_entity_id': ','.join(self.config['optimise_' + c] for c in ('import', 'export')), 'end_time': iso(now)})
        records = self.ha('/api/history/period/' + start + '?' + query)
        count = 0
        for stream in records:
            for state in stream:
                channel = next((c for c in ('import', 'export') if state.get('entity_id') == self.config['optimise_' + c]), None)
                if channel is None or state.get('state') in ('unknown', 'unavailable'):
                    continue
                attrs = state.get('attributes', {})
                point = attrs.get('period', {})
                observed_at = state.get('last_updated', state.get('last_changed'))
                if not point or not observed_at or not point.get('valid_to'):
                    continue
                when = instant(observed_at)
                if instant(point['valid_from']) <= when < instant(point['valid_to']):
                    rows = parse_optimise({'attributes': {**attrs, 'forecast': []}}, when, self.observed[channel])
                    count += len(rows)
        save(self.directory / 'history-seeded.json', {'at': iso(now), 'observed_records': count})

    def cycle(self):
        now = datetime.now(UTC)
        errors = {}
        jobs = {
            'optimise': lambda: self.optimise(now),
            'agile_import': lambda: fetch_octopus(self.config['agile_import_product'], now),
            'agile_export': lambda: fetch_octopus(self.config['agile_export_product'], now),
            'wholesale': lambda: fetch_neso_n2ex(now),
            'wholesale_intraday': lambda: fetch_elexon_mid(now),
        }
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {}
            for name, function in jobs.items():
                cached = self.cache.get(name, {})
                if cached.get('fetched_at') and 0 <= (now - instant(cached['fetched_at'])).total_seconds() < POLL_SECONDS[name]:
                    continue
                futures[name] = pool.submit(function)
            for name, future in futures.items():
                try:
                    self.cache[name] = future.result()
                except Exception as error:
                    errors[name] = type(error).__name__
        published_at = datetime.now(UTC)
        if (int(published_at.timestamp()) // 1800) != (int(now.timestamp()) // 1800):
            try:
                self.cache['optimise'] = self.optimise(published_at)
                errors.pop('optimise', None)
            except Exception as error:
                errors['optimise'] = type(error).__name__
        now = published_at
        for channel in ('import', 'export'):
            self.observed[channel] = {k: v for k, v in self.observed[channel].items() if instant(v['end']) > now - timedelta(days=7)}
        save(self.directory / 'cache.json', self.cache)
        save(self.directory / 'observed.json', self.observed)
        sources, series = {}, {}
        for name in jobs:
            feed = self.cache.get(name, {})
            fresh = source_is_fresh(name, feed, now)
            intervals = feed.get('intervals', [])
            all_intervals = intervals if name != 'optimise' else feed.get('import', [])
            sources[name] = {'status': 'ok' if fresh and name not in errors else ('cached' if fresh else 'stale'),
                             'last_success': feed.get('provider_last_success', feed.get('fetched_at')),
                             'forecast_until': max((p['end'] for p in all_intervals), default=None) if name != 'wholesale_intraday' else None,
                             'last_period_end': max((p['end'] for p in all_intervals), default=None),
                             'error_category': errors.get(name), 'url': feed.get('url'), 'product': feed.get('product')}
            if name == 'optimise':
                for channel in ('import', 'export'):
                    merged = dict(self.observed[channel])
                    if fresh:
                        merged.update({p['start']: p for p in feed.get(channel, []) if p.get('quality') == 'observed' or instant(p['start']) >= now})
                    series['optimise_' + channel] = sorted(merged.values(), key=lambda p: p['start'])
            else:
                series[name] = intervals if fresh else [p for p in intervals if instant(p['end']) <= now]
        sources['agile'] = {**sources['agile_import'], 'status': 'ok' if all(sources[k]['status'] == 'ok' for k in ('agile_import', 'agile_export')) else 'partial'}
        raw_rows = align(series, now - timedelta(hours=48), now + timedelta(hours=48))
        rows = apply_customer_pricing(raw_rows, self.customer_policy)
        attrs = {'friendly_name': 'Optimise / Agile price comparison', 'updated_at': iso(now),
                 'region': 'North West (G)', 'wholesale_label': 'Nord Pool N2EX day-ahead (hourly)',
                 'rows': rows, 'sources': sources, 'summary': paired_summary(rows, now),
                 'raw_summary': paired_summary(raw_rows, now),
                 'price_basis': 'customer_effective' if self.customer_policy else 'eon_api_raw',
                 'customer_pricing': self.customer_policy.metadata() if self.customer_policy else None,
                 'notes': ['Wholesale is Nord Pool N2EX hourly day-ahead, not Agile’s EPEX 30-minute auction.',
                           'Optional Elexon APX MID is an intraday market index, not a day-ahead forecast.',
                           'Retail rates use supplier p/kWh including applicable VAT; wholesale GBP/MWh is divided by 10 and excludes retail costs.',
                           'Future Optimise prices are revisable forecasts. Observed quotes are not verified settlement bills.',
                           'Missing periods remain gaps. Price differences exclude standing charges and actual consumption.'] + customer_pricing_notes(self.customer_policy)}
        current = next((r for r in rows if instant(r['start']) <= now < instant(r['end'])), {})
        upcoming = next((r for r in rows if r['start'] == current.get('end')), {})
        values = {key: current.get(key) for key in ('agile_import', 'agile_export')}
        values['wholesale_index'] = current.get('wholesale')
        for channel in ('import', 'export'):
            # Existing scalar IDs retain the raw historical basis. New effective
            # IDs start a clean history rather than relabelling old observations.
            raw = current.get('optimise_' + channel + '_raw')
            effective = current.get('optimise_' + channel)
            values['optimise_' + channel] = raw
            values['optimise_' + channel + '_effective'] = effective
            values['optimise_next_' + channel] = upcoming.get('optimise_' + channel)
            values['optimise_' + channel + '_raw'] = raw
            values['optimise_' + channel + '_adjustment'] = current.get('optimise_' + channel + '_adjustment')
            agile = current.get('agile_' + channel)
            values[channel + '_difference'] = round(raw - agile, 5) if raw is not None and agile is not None else None
            values[channel + '_difference_effective'] = round(effective - agile, 5) if effective is not None and agile is not None else None
        for name, value in values.items():
            label = name.replace('_', ' ').title()
            scalar_attrs = {'friendly_name': label, 'unit_of_measurement': 'p/kWh', 'state_class': 'measurement', 'updated_at': iso(now)}
            if name.startswith('optimise_') or name in ('import_difference', 'export_difference', 'import_difference_effective', 'export_difference_effective'):
                scalar_attrs['price_basis'] = ('eon_api_raw' if name.endswith('_raw') or name in ('optimise_import', 'optimise_export', 'import_difference', 'export_difference') else
                                               'customer_adjustment' if name.endswith('_adjustment') else attrs['price_basis'])
                scalar_attrs['customer_pricing_rule'] = attrs['customer_pricing']['rule_id'] if self.customer_policy else None
                scalar_attrs['provider_last_success'] = sources['optimise']['last_success']
                if self.customer_policy:
                    scalar_attrs.update(customer_pricing_start_date=attrs['customer_pricing']['start_date'],
                                        customer_pricing_end_date_exclusive=attrs['customer_pricing']['end_date_exclusive'],
                                        customer_pricing_timezone=attrs['customer_pricing']['timezone'])
                interval = upcoming if name.startswith('optimise_next_') else current
                scalar_attrs.update(valid_from=interval.get('start'), valid_to=interval.get('end'))
            self.ha('/api/states/' + PREFIX + name, {'state': 'unavailable' if value is None else value,
                    'attributes': scalar_attrs})
        health = 'ready' if not errors and all(sources[k]['status'] == 'ok' for k in jobs) else 'partial'
        self.ha('/api/states/sensor.optimise_agile_comparison', {'state': health, 'attributes': attrs})
        save(self.directory / 'last-cycle.json', {'at': iso(now), 'health': health, 'errors': errors, 'sources': sources, 'summary': attrs['summary'], 'raw_summary': attrs['raw_summary'], 'customer_pricing': attrs['customer_pricing'], 'current': values, 'rows': len(rows)})
        print(json.dumps({'at': iso(now), 'health': health, 'errors': errors, 'intervals': len(rows), 'current': values}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--seed-history', action='store_true')
    args = parser.parse_args()
    collector = Collector(json.loads(args.config.read_text()))
    lock = (collector.directory / 'collector.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Comparison collector is already running')
    if args.seed_history:
        collector.seed_history(datetime.now(UTC))
    while True:
        try:
            collector.cycle()
        except Exception as error:
            print(json.dumps({'at': iso(datetime.now(UTC)), 'error': type(error).__name__}), flush=True)
            if not args.watch:
                raise SystemExit(1)
        if not args.watch:
            break
        # Align clock-driven current prices with the half-hour boundaries.
        time.sleep(300 - (time.time() % 300) + 2)


if __name__ == '__main__':
    main()
