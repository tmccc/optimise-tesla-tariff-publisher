"""Build a dated, guarded tariff proposal without network or control actions.

The output always requires an independently approved expiry policy. Tesla's
month/day seasons repeat annually; they are not a server-enforced lease.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from tariff_schema import canonical_hash, to_upload_tariff, validate_tariff
from planning_pricing import (normalize_policy, policy_hash, overlay_pattern,
                              planning_quote_rows)

UTC = timezone.utc
LONDON = ZoneInfo("Europe/London")
REFERENCE = date(2000, 1, 1)  # Include Feb 29 in the annual schedule.
POLICIES = {"preserve_baseline", "disabled_zero_placeholder", "frozen_current_quote"}


def _instant(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("aware_time_required")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _price(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("invalid_price")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("invalid_price") from None
    if not result.is_finite() or result < 0 or result > Decimal("10000"):
        raise ValueError("invalid_price")
    if len(result.as_tuple().digits) > 18 or result.as_tuple().exponent < -18:
        raise ValueError("invalid_price")
    return result


def _md(value: date) -> int:
    return (date(2000, value.month, value.day) - REFERENCE).days


def _season_on(part: dict, day: date) -> tuple[str, dict]:
    index = _md(day)
    for name, season in part["seasons"].items():
        first = _md(date(2000, season["fromMonth"], season["fromDay"]))
        last = _md(date(2000, season["toMonth"], season["toDay"]))
        if (first <= index <= last) if first <= last else (index >= first or index <= last):
            return name, season
    raise ValueError("baseline_season_missing")


def _baseline_pattern(part: dict, day: date) -> tuple[tuple[int, int, Decimal], ...]:
    """Resolve a structurally validated all-week baseline without sampling it."""
    name, season = _season_on(part, day)
    charges = part.get("energy_charges", {})
    local = charges.get(name, {})
    local = local.get("rates", local)
    all_charges = charges.get("ALL", {})
    all_charges = all_charges.get("rates", all_charges)
    global_flat = all_charges.get("ALL")
    flat = local.get("ALL") if set(local) == {"ALL"} else None
    segments = []
    for label, periods in season["tou_periods"].items():
        amount = local.get(label, flat if flat is not None else global_flat)
        if amount is None:
            raise ValueError("baseline_price_missing")
        if isinstance(periods, dict):
            periods = periods.get("periods")
        for period in periods:
            if period["fromDayOfWeek"] != 0 or period["toDayOfWeek"] != 6:
                raise ValueError("baseline_weekday_pattern_unsupported")
            start = period["fromHour"] * 60 + period.get("fromMinute", 0)
            end = period["toHour"] * 60 + period.get("toMinute", 0)
            if end == 0:
                end = 1440
            if start < end:
                segments.append((start, end, _price(amount)))
            else:
                segments.extend(((start, 1440, _price(amount)), (0, end, _price(amount))))
    return _merge(sorted(segments))


def _merge(segments: list[tuple[int, int, Decimal]]) -> tuple[tuple[int, int, Decimal], ...]:
    merged = []
    for start, end, price in segments:
        if start == end:
            continue
        if merged and merged[-1][1] == start and merged[-1][2] == price:
            merged[-1] = (merged[-1][0], end, price)
        else:
            merged.append((start, end, price))
    if not merged or merged[0][0] != 0 or merged[-1][1] != 1440:
        raise ValueError("clock_coverage_invalid")
    if any(previous[1] != current[0] for previous, current in zip(merged, merged[1:])):
        raise ValueError("clock_coverage_invalid")
    return tuple(merged)


def _overlay(base: tuple, quotes: dict[int, tuple[Decimal, Decimal]], direction: int) -> tuple:
    boundaries = {0, 1440}
    for start, end, _ in base:
        boundaries.update((start, end))
    for minute in quotes:
        boundaries.update((minute, minute + 30))
    points = sorted(boundaries)
    segments = []
    for start, end in zip(points, points[1:]):
        quote = quotes.get((start // 30) * 30)
        price = quote[direction] if quote is not None else next(
            price for first, last, price in base if first <= start < last
        )
        segments.append((start, end, price))
    return _merge(segments)


def _number(value: Decimal) -> int | float:
    result = int(value) if value == value.to_integral_value() else float(value)
    if Decimal(str(result)) != value:
        raise ValueError("json_price_precision_loss")
    return result


def _compile_channel(original: dict, days: list[tuple], *, placeholder: bool) -> dict:
    result = deepcopy(original)
    result.pop("sell_tariff", None)
    result.update({"utility": "E.ON Next", "code": "HA_EON_OPTIMISE_TEMP",
                   "name": "Next Optimise (temporary HA prices)"})
    result["currency"] = "GBP"
    result["seasons"] = {}
    result["energy_charges"] = {}
    # Only zero demand/daily charges are accepted by the input validator.
    result["demand_charges"] = {"ALL": {"ALL": 0}}
    if "daily_charges" in result:
        result["daily_charges"] = {"ALL": {"ALL": 0}}
    groups = []
    for index, pattern in enumerate(days):
        if groups and groups[-1][2] == pattern:
            groups[-1] = (groups[-1][0], index, pattern)
        else:
            groups.append((index, index, pattern))
    for first, last, pattern in groups:
        start, end = REFERENCE + timedelta(days=first), REFERENCE + timedelta(days=last)
        name = f"HA_{start:%m%d}_{end:%m%d}"
        season = {"fromMonth": start.month, "fromDay": start.day,
                  "toMonth": end.month, "toDay": end.day, "tou_periods": {}}
        rates = {}
        for lower, upper, price in pattern:
            label = f"T{lower:04d}_{upper:04d}"
            season["tou_periods"][label] = [{
                "fromDayOfWeek": 0, "toDayOfWeek": 6,
                "fromHour": lower // 60, "fromMinute": lower % 60,
                "toHour": (upper // 60) % 24, "toMinute": upper % 60,
            }]
            rates[label] = _number(price)
        result["seasons"][name] = season
        result["energy_charges"][name] = rates
    return result


def build_date_candidate(preview: dict, baseline: dict, *, now: datetime,
                         unquoted_policy: str = "preserve_baseline",
                         planning_policy: dict | None = None) -> dict:
    """Overlay quoted intervals by date; never activate or invent a price quote.

    ``disabled_zero_placeholder`` is an explicit *proposal*, not a safe operating
    fallback: zero/zero entries outside the quote window must never drive Savings.
    Those future zeros can influence Savings lookahead even before expiry.
    ``frozen_current_quote`` instead repeats the first/current observed pair in
    unquoted slots. That is a static assumption, not future quotes or history.
    Both proposals require approval and an independent handoff before expiry.
    """
    result = {"schema_version": 1, "tariff": None, "manifest": {}, "errors": [],
              "candidate_valid": False, "ready_for_upload": False}

    def error(code: str, **details: Any) -> None:
        result["errors"].append({"code": code, **details})

    try:
        clock = _instant(now)
    except (ValueError, TypeError, OverflowError):
        error("invalid_assessment_time")
        return result
    if unquoted_policy not in POLICIES:
        error("unknown_unquoted_policy")
        return result
    try:
        planning_policy=normalize_policy(planning_policy)
    except (ValueError,TypeError,KeyError,OverflowError):
        error('planning_policy_invalid')
        return result
    if planning_policy is not None and preview.get('price_basis') not in (None,'raw_eon_optimise_api'):
        error('planning_source_already_adjusted')
        return result
    if planning_policy is not None and unquoted_policy!='frozen_current_quote':
        error('planning_policy_requires_frozen_quote')
        return result
    if not isinstance(preview, dict) or preview.get("valid") is not True or preview.get("errors"):
        error("preview_not_validated")
        return result
    if preview.get("currency") != "GBP" or preview.get("timezone") != "Europe/London":
        error("unsupported_currency_or_timezone")
        return result
    for field in ("source_last_success", "source_captured_at"):
        try:
            age = (clock - _instant(preview.get(field))).total_seconds()
        except (ValueError, TypeError, OverflowError):
            error("source_timestamp_invalid", field=field)
            continue
        if not -30 <= age < 900:
            error("source_not_fresh", field=field)
    baseline_errors = validate_tariff(baseline, strict_compatibility=False)
    if baseline_errors:
        error("baseline_structure_invalid", validation=baseline_errors)
    if result["errors"]:
        return result
    periods = preview.get("periods")
    if not isinstance(periods, list) or not 1 <= len(periods) <= 200:
        error("invalid_period_count")
        return result
    quotes = []
    for row in periods:
        try:
            if not isinstance(row, dict):
                raise ValueError("invalid_period")
            start, end = _instant(row.get("valid_from")), _instant(row.get("valid_to"))
            if end - start != timedelta(minutes=30) or start.minute not in (0, 30) or start.second or start.microsecond:
                raise ValueError("invalid_interval")
            if end <= clock:
                continue
            buy, sell = _price(row.get("import_gbp_per_kwh")), _price(row.get("export_gbp_per_kwh"))
            if buy < sell:
                raise ValueError("export_exceeds_import")
            for field, price in (("import_p_per_kwh", buy), ("export_p_per_kwh", sell)):
                if field in row and _price(row[field]) / 100 != price:
                    raise ValueError("inconsistent_money_units")
            quotes.append((start, end, buy, sell))
        except (ValueError, TypeError, OverflowError) as exc:
            known = {"invalid_period", "invalid_interval", "invalid_price",
                     "export_exceeds_import", "inconsistent_money_units", "aware_time_required"}
            error(str(exc) if str(exc) in known else "invalid_period")
    quotes.sort(key=lambda row: row[0])
    if not quotes:
        error("no_current_or_future_quotes")
    elif not quotes[0][0] <= clock < quotes[0][1]:
        error("current_quote_missing")
    if any(previous[1] != current[0] for previous, current in zip(quotes, quotes[1:])):
        error("quote_gap_or_overlap")
    if quotes and quotes[-1][1] - clock > timedelta(hours=72):
        error("quote_horizon_too_long")
    if result["errors"]:
        return result
    by_date: dict[date, dict[int, tuple[Decimal, Decimal]]] = {}
    folds: dict[tuple[date, int], set[int]] = {}
    nonexistent = []
    for start, end, buy, sell in quotes:
        local = start.astimezone(LONDON)
        minute = local.hour * 60 + local.minute
        key = (local.date(), minute)
        existing = by_date.setdefault(local.date(), {}).get(minute)
        if existing is not None and existing != (buy, sell):
            error("ambiguous_dst_rates", local_time=local.replace(tzinfo=None).isoformat())
        by_date[local.date()][minute] = (buy, sell)
        folds.setdefault(key, set()).add(local.fold)
        # A UTC half-hour ending across spring's jump covers only 30 wall-clock
        # minutes from its start. The skipped hour remains explicitly unquoted.
        wall_end = minute + 30
        end_local = end.astimezone(LONDON)
        if end_local.date() == local.date() and end_local.hour * 60 + end_local.minute > wall_end:
            nonexistent.append({"date": local.date().isoformat(), "from_minute": wall_end,
                                "to_minute": end_local.hour * 60 + end_local.minute})
        first = local.replace(fold=0).astimezone(UTC)
        second = local.replace(fold=1).astimezone(UTC)
        if first != second:
            folds.setdefault(key, set())
    for (day, minute), present in folds.items():
        local = datetime(day.year, day.month, day.day, minute // 60, minute % 60, tzinfo=LONDON)
        if local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset() and present != {0, 1}:
            error("partial_dst_fold_coverage", date=day.isoformat(), minute=minute)
    # Month/day seasons cannot distinguish two quote dates in different years.
    if len({_md(day) for day in by_date}) != len(by_date):
        error("annual_date_alias")
    if result["errors"]:
        return result
    placeholder = unquoted_policy == "disabled_zero_placeholder"
    frozen = unquoted_policy == "frozen_current_quote"
    assumed_pair = (quotes[0][2], quotes[0][3]) if frozen else (Decimal(0), Decimal(0))
    channels = []
    try:
        for direction, original in enumerate((baseline, baseline["sell_tariff"])):
            patterns = []
            for index in range(366):
                day = REFERENCE + timedelta(days=index)
                base = ((0, 1440, assumed_pair[direction]),) if placeholder or frozen else _baseline_pattern(original, day)
                current_day = next((key for key in by_date if _md(key) == index), None)
                if current_day is not None:
                    base = _overlay(base, by_date[current_day], direction)
                if planning_policy is not None:
                    # Only actual quoted dates receive adjustments. The
                    # reference date is inert when quoted_minutes is empty.
                    actual_day=current_day or day
                    base=_merge(overlay_pattern(base,day=actual_day,direction=direction,
                        policy=planning_policy,quoted_minutes=by_date.get(current_day,{})))
                patterns.append(base)
            channels.append(_compile_channel(original, patterns, placeholder=placeholder))
    except (ValueError, TypeError, KeyError, StopIteration, OverflowError) as exc:
        error('planning_negative_price' if str(exc)=='planning_negative_price' else "baseline_overlay_unsupported")
        return result
    candidate = channels[0]
    candidate["sell_tariff"] = channels[1]
    if planning_policy is not None:
        for part in (candidate,candidate['sell_tariff']):
            part['name']='Next Optimise (HA planning rates)'
            part['code']='HA_EON_OPTIMISE_PLANNING'
    candidate = to_upload_tariff(candidate)
    strict_errors = validate_tariff(candidate, strict_compatibility=True)
    if strict_errors:
        error("candidate_tesla_validation_failed", validation=strict_errors)
    result["tariff"] = candidate
    result["candidate_valid"] = not result["errors"]
    result["manifest"] = {
        "created_at": _iso(clock), "timezone": "Europe/London", "currency": "GBP",
        "known_valid_from": _iso(quotes[0][0]), "known_valid_until": _iso(quotes[-1][1]),
        "quoted_interval_count": len(quotes), "quoted_dates": [day.isoformat() for day in sorted(by_date)],
        "quoted_intervals": [{"valid_from": _iso(start), "valid_to": _iso(end),
                              "import_gbp_per_kwh": str(buy), "export_gbp_per_kwh": str(sell)}
                             for start, end, buy, sell in quotes],
        "source_last_success": preview["source_last_success"],
        "source_captured_at": preview["source_captured_at"],
        "unquoted_policy": unquoted_policy,
        "unquoted_prices_are_optimise_quotes": False,
        "elapsed_clock_prices_are_historical_optimise": False,
        "unquoted_lookahead_assumption_requires_approval": True,
        "unquoted_static_pair_is_observed_current_quote": frozen,
        "unquoted_static_pair_is_future_forecast": False,
        "unquoted_static_pair_is_historical_price": False,
        "unquoted_assumed_pair": {
            "observed_valid_from": _iso(quotes[0][0]),
            "observed_valid_to": _iso(quotes[0][1]),
            "import_gbp_per_kwh": str(assumed_pair[0]),
            "export_gbp_per_kwh": str(assumed_pair[1]),
            "import_p_per_kwh": str(assumed_pair[0] * 100),
            "export_p_per_kwh": str(assumed_pair[1] * 100),
        } if frozen else None,
        "unquoted_zero_prices_can_influence_savings_lookahead": placeholder,
        "activation_blockers": (["zero_placeholder_lookahead_unapproved"] if placeholder else [])
            + ["unquoted_lookahead_assumption_unapproved", "expiry_handoff_unapproved"],
        "source_prices_are_settlement": False,
        "billing_adjustments_applied": False,
        "supplier_link_reset_claimed": False,
        "unquoted_intervals_must_not_drive_savings": placeholder,
        "nonexistent_local_clock_intervals": nonexistent,
        "quoted_dates_repeat_annually": True,
        "tesla_enforces_expiry": False,
        "independent_expiry_guard_required": True,
        "required_approvals": ["unquoted_policy", "unquoted_lookahead_assumption", "expiry_self_powered_handoff"]
                              if placeholder or frozen else ["unquoted_policy", "unquoted_lookahead_assumption", "expiry_fallback"],
        "baseline_sha256": canonical_hash(baseline), "tariff_sha256": canonical_hash(candidate),
        "controls_changed": False, "tesla_requests_sent": 0,
    }
    if planning_policy is not None:
        result['manifest'].update(planning_policy=planning_policy,
            planning_policy_sha256=policy_hash(planning_policy),
            planning_quoted_intervals=planning_quote_rows(result['manifest']['quoted_intervals'],planning_policy,LONDON),
            planning_prices_are_billing_prices=False,
            planning_price_basis='tesla_earlybird_approximation',
            planning_export_understatement_p_per_kwh=3,
            planning_adjustment_scope='quoted_intervals_only',
            planning_unquoted_scope=planning_policy['unquoted_scope'])
    return result
