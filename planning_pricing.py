"""Pure, explicit Tesla-only price approximation over raw E.ON prices.

This is deliberately separate from customer billing adjustments. At night the
real export price is unchanged, while the authorised planning approximation
reduces both buy and sell. No price is silently clamped for Tesla.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import re

FIELDS = {
    'schema_version', 'kind', 'rule_id', 'timezone', 'start_date',
    'end_date_exclusive', 'overnight_reduction_p_per_kwh',
    'peak_export_bonus_p_per_kwh', 'source_price_basis', 'unquoted_scope',
}


def normalize_policy(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError('planning_policy_fields_invalid')
    if (value['schema_version'] != 1 or value['kind'] != 'eon_earlybird_planning_v1'
            or value['timezone'] != 'Europe/London'
            or value['source_price_basis'] != 'raw_eon_optimise_api'
            or value['unquoted_scope'] != 'quoted_only'):
        raise ValueError('planning_policy_basis_invalid')
    if not isinstance(value['rule_id'], str) or not re.fullmatch(r'[a-z0-9-]{1,100}', value['rule_id']):
        raise ValueError('planning_policy_rule_invalid')
    start, end = date.fromisoformat(value['start_date']), date.fromisoformat(value['end_date_exclusive'])
    if not start < end or (end-start).days > 367:
        raise ValueError('planning_policy_dates_invalid')
    for field in ('overnight_reduction_p_per_kwh', 'peak_export_bonus_p_per_kwh'):
        if type(value[field]) not in (int, float) or Decimal(str(value[field])) != Decimal(3):
            raise ValueError('planning_policy_amount_invalid')
    return deepcopy(value)


def policy_hash(policy):
    return hashlib.sha256(json.dumps(normalize_policy(policy), sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def adjustment(policy, day, minute, direction):
    """Return GBP/kWh offset using the explicitly selected local date/clock."""
    if not date.fromisoformat(policy['start_date']) <= day < date.fromisoformat(policy['end_date_exclusive']):
        return Decimal(0)
    if 0 <= minute < 360:
        return -Decimal(str(policy['overnight_reduction_p_per_kwh'])) / 100
    if direction == 1 and 960 <= minute < 1140:
        return Decimal(str(policy['peak_export_bonus_p_per_kwh'])) / 100
    return Decimal(0)


def overlay_pattern(pattern, *, day, direction, policy, quoted_minutes):
    """Split only after the raw quote/frozen baseline pattern has been filled."""
    boundaries = {0, 360, 960, 1140, 1440}
    for start, end, _ in pattern:
        boundaries.update((start, end))
    for minute in quoted_minutes:
        boundaries.update((minute, minute + 30))
    points=sorted(boundaries)
    result=[]
    for start,end in zip(points,points[1:]):
        raw=next(price for lower,upper,price in pattern if lower<=start<upper)
        quoted=(start//30)*30 in quoted_minutes
        offset=adjustment(policy,day,start,direction) if quoted else Decimal(0)
        price=raw+offset
        if price < 0:
            raise ValueError('planning_negative_price')
        result.append((start,end,price))
    return result


def planning_quote_rows(rows, policy, london):
    result=[]
    for original in rows:
        row=deepcopy(original)
        start=datetime.fromisoformat(row['valid_from'].replace('Z','+00:00')).astimezone(london)
        minute=start.hour*60+start.minute
        for direction,name in enumerate(('import','export')):
            raw=Decimal(str(row[name+'_gbp_per_kwh']))
            row[name+'_gbp_per_kwh']=str(raw+adjustment(policy,start.date(),minute,direction))
        result.append(row)
    return result
