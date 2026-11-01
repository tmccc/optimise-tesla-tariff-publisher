"""Bounded, pure validation and evaluation of this publisher's Tesla tariffs.

Season end dates are inclusive annual month/day ranges. Only all-week 0..6
periods are supported; no weekday-numbering convention is guessed. An end
time of 00:00 means the following midnight, including 00:00..00:00 (all day).
Other overnight clock ranges are deliberately unsupported rather than guessed.

This module validates the supported residential energy-price subset, not every
tariff Tesla might accept. Nonzero demand/daily charges and ambiguous compound
charge structures are rejected. Structural validation of a captured tariff does
not establish that uploading it will preserve supplier-link state.
"""
from __future__ import annotations

from dataclasses import dataclass
import copy
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any
from zoneinfo import ZoneInfo

MAX_SEASONS = 366
MAX_PERIODS = 4096
MAX_PRICE = Decimal('10000')
_DAYS = tuple(date(2000, 1, 1) + timedelta(days=i) for i in range(366))


class TariffValidationError(ValueError):
    """Only fixed codes are carried, never arbitrary tariff/provider text."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(';'.join(errors))


def _price(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError('invalid_price')
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError('invalid_price') from None
    if not out.is_finite() or out < 0 or out > MAX_PRICE:
        raise ValueError('invalid_price')
    if len(out.as_tuple().digits) > 24 or out.as_tuple().exponent < -18:
        raise ValueError('invalid_price')
    return out


def _integer(value: Any, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError('invalid_integer')
    return value


def _ordinal(month: Any, day: Any) -> int:
    _integer(month, 1, 12)
    _integer(day, 1, 31)
    return date(2000, month, day).timetuple().tm_yday


def _in_season(ordinal: int, start: int, end: int) -> bool:
    return start <= ordinal <= end if start <= end else ordinal >= start or ordinal <= end


@dataclass(frozen=True)
class _Season:
    start: int
    end: int
    # Seven independently complete schedules. Entries are (start,end,price).
    days: tuple[tuple[tuple[int, int, Decimal], ...], ...]


def _zero_charges(value: Any) -> bool:
    """Preserve zero ALL/ALL placeholders; never ignore actual fixed charges."""
    if value is None:
        return True
    try:
        value = _charge_rows(value)
    except ValueError:
        return False
    for row in value.values():
        if not isinstance(row, dict) or len(row) > MAX_PERIODS:
            return False
        for item in row.values():
            try:
                if _price(item) != 0:
                    return False
            except ValueError:
                return False
    return True


def _charge_rows(value: Any) -> dict:
    """Accept either GET flat rates or POST rates wrappers, never a mixture."""
    if not isinstance(value, dict) or len(value) > MAX_SEASONS + 1:
        raise ValueError('invalid_charge_table')
    output = {}
    formats = set()
    for name, row in value.items():
        if not isinstance(name, str) or not name or not isinstance(row, dict):
            raise ValueError('invalid_charge_table')
        if 'rates' in row:
            # Arbitrary siblings could be metadata or flat prices. Never drop
            # or guess their interpretation during an upload conversion.
            if set(row) != {'rates'} or not isinstance(row['rates'], dict):
                raise ValueError('mixed_charge_formats')
            output[name] = row['rates']
            formats.add('wrapped')
        else:
            output[name] = row
            if row:
                formats.add('flat')
        if len(output[name]) > MAX_PERIODS:
            raise ValueError('invalid_charge_table')
    if len(formats) > 1:
        raise ValueError('mixed_charge_formats')
    return output


def _period_groups(value: Any) -> dict:
    """Accept GET lists or POST periods wrappers without deleting siblings."""
    if not isinstance(value, dict) or not 1 <= len(value) <= MAX_PERIODS:
        raise ValueError('invalid_tou_periods')
    output = {}
    formats = set()
    for label, rows in value.items():
        if not isinstance(label, str) or not label:
            raise ValueError('invalid_tou_label')
        if isinstance(rows, dict):
            if set(rows) != {'periods'} or not isinstance(rows['periods'], list):
                raise ValueError('mixed_tou_formats')
            output[label] = rows['periods']
            formats.add('wrapped')
        elif isinstance(rows, list):
            output[label] = rows
            formats.add('flat')
        else:
            raise ValueError('invalid_tou_rows')
    if len(formats) > 1:
        raise ValueError('mixed_tou_formats')
    return output


def _channel(tariff: Any, prefix: str, errors: set[str]) -> tuple[_Season, ...]:
    def error(code: str) -> None:
        errors.add(prefix + '.' + code)

    if not isinstance(tariff, dict):
        error('not_object')
        return ()
    if tariff.get('currency') != 'GBP':
        error('unsupported_currency')
    if 'version' in tariff and (type(tariff['version']) is not int or tariff['version'] != 1):
        error('unsupported_version')
    if not _zero_charges(tariff.get('demand_charges')):
        error('unsupported_demand_charges')
    if not _zero_charges(tariff.get('daily_charges')):
        error('unsupported_daily_charges')
    seasons = tariff.get('seasons')
    try:
        charges = _charge_rows(tariff.get('energy_charges'))
    except ValueError as exc:
        error(str(exc))
        return ()
    if not isinstance(seasons, dict) or not 1 <= len(seasons) <= MAX_SEASONS:
        error('invalid_seasons')
        return ()
    if not charges:
        error('invalid_energy_charges')
        return ()
    if any(not isinstance(name, str) or not name for name in seasons):
        error('invalid_season_name')
        return ()
    if set(charges) - set(seasons) - {'ALL'}:
        error('extra_charge_season')
    global_rate = None
    if 'ALL' in charges:
        row = charges['ALL']
        if not isinstance(row, dict) or set(row) != {'ALL'}:
            error('invalid_global_energy_charge')
        else:
            try:
                global_rate = _price(row['ALL'])
            except ValueError:
                error('invalid_energy_price')
    compiled = []
    total_periods = 0
    for name, season in seasons.items():
        if not isinstance(season, dict):
            error('invalid_season')
            continue
        try:
            start = _ordinal(season.get('fromMonth'), season.get('fromDay'))
            end = _ordinal(season.get('toMonth'), season.get('toDay'))
        except (ValueError, TypeError):
            error('invalid_season_date')
            continue
        try:
            groups = _period_groups(season.get('tou_periods'))
        except ValueError as exc:
            error(str(exc))
            continue
        prices = charges.get(name, {})
        if not isinstance(prices, dict) or len(prices) > MAX_PERIODS:
            error('invalid_season_charges')
            continue
        # A flat ALL charge is supported. Compound nonzero charges are not
        # evaluated using guessed addition/override semantics.
        if set(prices) - set(groups) - {'ALL'}:
            error('extra_price_label')
        if 'ALL' in prices and len(prices) != 1:
            error('ambiguous_charge_composition')
        if global_rate not in (None, Decimal(0)) and prices:
            error('ambiguous_charge_composition')
        fallback = global_rate
        if 'ALL' in prices:
            try:
                fallback = _price(prices['ALL'])
            except ValueError:
                error('invalid_energy_price')
        days: list[list[tuple[int, int, Decimal]]] = [[] for _ in range(7)]
        for label, rows in groups.items():
            try:
                price = _price(prices[label]) if label in prices else fallback
                if price is None:
                    error('missing_price_label')
                    continue
            except ValueError:
                error('invalid_energy_price')
                continue
            if not isinstance(rows, list) or not rows or len(rows) > MAX_PERIODS:
                error('invalid_tou_rows')
                continue
            total_periods += len(rows)
            if total_periods > MAX_PERIODS:
                error('too_many_periods')
                return ()
            for row in rows:
                if not isinstance(row, dict):
                    error('invalid_tou_row')
                    continue
                try:
                    first = _integer(row.get('fromDayOfWeek'), 0, 6)
                    last = _integer(row.get('toDayOfWeek'), 0, 6)
                    if first != 0 or last != 6:
                        error('weekday_specific_unsupported')
                    begin = _integer(row.get('fromHour'), 0, 23) * 60 + _integer(row.get('fromMinute', 0), 0, 59)
                    finish = _integer(row.get('toHour'), 0, 23) * 60 + _integer(row.get('toMinute', 0), 0, 59)
                    finish = finish if finish else 1440
                    if finish <= begin:
                        raise ValueError('unsupported_overnight')
                except ValueError:
                    error('invalid_tou_range')
                    continue
                weekdays = range(first, last + 1) if first <= last else (*range(first, 7), *range(last + 1))
                for weekday in weekdays:
                    days[weekday].append((begin, finish, price))
        for rows in days:
            rows.sort(key=lambda item: (item[0], item[1]))
            cursor = 0
            for begin, finish, _ in rows:
                if begin < cursor:
                    error('tou_overlap')
                if begin > cursor:
                    error('tou_gap')
                cursor = max(cursor, finish)
            if cursor != 1440:
                error('tou_gap')
        compiled.append(_Season(start, end, tuple(tuple(rows) for rows in days)))
    # Annual coverage includes 29 February so a latent leap-year hole is caught.
    for day in _DAYS:
        ordinal = day.timetuple().tm_yday
        count = sum(_in_season(ordinal, s.start, s.end) for s in compiled)
        if count == 0:
            error('season_gap')
        elif count > 1:
            error('season_overlap')
    return tuple(compiled)


def _season_at(model: tuple[_Season, ...], ordinal: int) -> _Season:
    matches = [s for s in model if _in_season(ordinal, s.start, s.end)]
    if len(matches) != 1:
        raise TariffValidationError(['season_not_unique'])
    return matches[0]


def _at(season: _Season, weekday: int, minute: int) -> Decimal:
    matches = [rate for begin, end, rate in season.days[weekday] if begin <= minute < end]
    if len(matches) != 1:
        raise TariffValidationError(['period_not_unique'])
    return matches[0]


def _models(tariff: Any) -> tuple[tuple[_Season, ...], tuple[_Season, ...], set[str]]:
    errors: set[str] = set()
    buy = _channel(tariff, 'import', errors)
    sell = _channel(tariff.get('sell_tariff') if isinstance(tariff, dict) else None, 'export', errors)
    if isinstance(tariff, dict) and isinstance(tariff.get('sell_tariff'), dict) and 'sell_tariff' in tariff['sell_tariff']:
        errors.add('export.nested_sell_tariff')
    return buy, sell, errors


def validate_tariff(tariff: Any, *, strict_compatibility: bool = True) -> list[str]:
    """Return fixed error codes; structural mode does not prove write safety.

    Strict mode additionally checks Tesla's buy >= sell requirement at every
    season, weekday and clock-rate boundary across a complete annual calendar.
    Both modes reject negative prices and unsupported fixed/demand charges.
    """
    buy, sell, errors = _models(tariff)
    if not errors and strict_compatibility:
        checked = set()
        for day in _DAYS:
            ordinal = day.timetuple().tm_yday
            imp, exp = _season_at(buy, ordinal), _season_at(sell, ordinal)
            identity = (id(imp), id(exp))
            if identity in checked:
                continue
            checked.add(identity)
            for weekday in range(7):
                boundaries = {start for start, _, _ in imp.days[weekday]}
                boundaries.update(start for start, _, _ in exp.days[weekday])
                if any(_at(imp, weekday, minute) < _at(exp, weekday, minute) for minute in boundaries):
                    errors.add('export_exceeds_import')
    return sorted(errors)


def rate_at(tariff: Any, instant: datetime, direction: str = 'import', timezone: str = 'Europe/London') -> Decimal:
    """Evaluate a complete structural tariff at an aware instant.

    A clock schedule cannot distinguish the two occurrences of a repeated DST
    hour: both folds intentionally receive the same clock rate. The compiler
    must reject source data requiring different rates for those occurrences.
    Existing incompatible buy/sell prices remain readable without clamping.
    """
    if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError('aware_datetime_required')
    if direction not in {'import', 'export'}:
        raise ValueError('invalid_direction')
    buy, sell, errors = _models(tariff)
    if errors:
        raise TariffValidationError(sorted(errors))
    local = instant.astimezone(ZoneInfo(timezone))
    ordinal = _ordinal(local.month, local.day)
    # Valid supported tariffs have the same all-week periods on every day.
    weekday = 0
    season = _season_at(buy if direction == 'import' else sell, ordinal)
    return _at(season, weekday, local.hour * 60 + local.minute)


def canonical_hash(tariff: Any) -> str:
    """Hash exact canonical JSON; this is not a semantic rollback guarantee."""
    def encode(value: Any):
        if isinstance(value, Decimal) and value.is_finite():
            return format(value.normalize(), 'f') if value else '0'
        raise TypeError('unsupported_canonical_value')
    encoded = json.dumps(tariff, sort_keys=True, separators=(',', ':'), allow_nan=False,
                         ensure_ascii=False, default=encode).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def to_upload_tariff(tariff: Any) -> dict:
    """Convert a structurally valid GET/POST tariff to documented POST shape.

    Existing root/season/period metadata and the complete sell tariff are kept
    through a deep copy. Unknown siblings inside reserved rates/periods wrappers
    are rejected by validation, never silently removed. This conversion does
    not fix crossed prices and does not establish that a restore is safe.
    """
    errors = validate_tariff(tariff, strict_compatibility=False)
    if errors:
        raise TariffValidationError(errors)
    result = copy.deepcopy(tariff)
    result.setdefault('version', 1)
    for channel in (result, result['sell_tariff']):
        for field in ('energy_charges', 'demand_charges', 'daily_charges'):
            if field in channel and channel[field] is not None:
                channel[field] = {name: {'rates': copy.deepcopy(rates)}
                                  for name, rates in _charge_rows(channel[field]).items()}
        for season in channel['seasons'].values():
            season['tou_periods'] = {label: {'periods': copy.deepcopy(rows)}
                                    for label, rows in _period_groups(season['tou_periods']).items()}
    return result


def semantic_hash(tariff: Any) -> str:
    """Hash full annual clock rates independently of labels and GET/POST shape.

    Metadata, season names and price-label names have no effect. Adjacent equal
    price segments and consecutive days with identical schedules are merged.
    Leap-day rates remain represented. Validation limits this digest to GBP,
    zero fixed/demand charges, all-week clock schedules and explicit prices.
    """
    buy, sell, errors = _models(tariff)
    if errors:
        raise TariffValidationError(sorted(errors))

    def schedules(model):
        runs = []
        for ordinal in range(1, 367):
            season = _season_at(model, ordinal)
            segments = []
            for start, end, rate in season.days[0]:
                encoded = format(rate.normalize(), 'f') if rate else '0'
                if segments and segments[-1][1] == start and segments[-1][2] == encoded:
                    segments[-1][1] = end
                else:
                    segments.append([start, end, encoded])
            if runs and runs[-1]['periods'] == segments:
                runs[-1]['to'] = ordinal
            else:
                runs.append({'from': ordinal, 'to': ordinal, 'periods': segments})
        return runs

    normalized = {'schema': 'annual-clock-rates-v1', 'currency': 'GBP',
                  'fixed_and_demand_charges': 'zero',
                  'import': schedules(buy), 'export': schedules(sell)}
    return canonical_hash(normalized)
