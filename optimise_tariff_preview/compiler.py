"""Validate an Optimise price preview without network access or tariff writes.

The compiler never invents prices or constructs a partly covered upload payload.
Money values in the report are decimal strings; all arithmetic uses Decimal.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc
MAX_PERIODS = 20_000
FRESHNESS_SECONDS = 900
HALF_HOUR = timedelta(minutes=30)


def _instant(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A timezone-aware timestamp is required")
    return value.astimezone(UTC)


def _money(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("A finite decimal price is required")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("A finite decimal price is required") from None
    if not result.is_finite() or result.copy_abs() > Decimal("1000000"):
        raise ValueError("A finite bounded decimal price is required")
    if result.as_tuple().exponent < -18 or len(result.as_tuple().digits) > 18:
        raise ValueError("The decimal price exceeds the supported exact precision")
    return result


def _decimal(value: Decimal) -> str:
    return format(value.normalize(), "f") if value else "0"


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _covered(periods: list[dict], start: datetime, end: datetime) -> bool:
    cursor = start
    for row in periods:
        if row["end"] <= cursor or row["start"] >= end:
            continue
        if row["start"] > cursor:
            return False
        cursor = max(cursor, row["end"])
        if cursor >= end:
            return True
    return False


def compile_preview(source: dict, *, now: datetime | None = None) -> dict:
    """Return a JSON-safe, non-uploadable validation report for canonical HA rows.

    Source keys: captured_at, now(optional), timezone, unit, currency,
    last_success, channels.import/export. Each period supplies valid_from,
    valid_to, value, quality, estimate and interval_basis. Export values are HA
    earnings values already; this function never changes their sign.
    """
    errors: list[dict] = []
    warnings: list[dict] = []
    blockers: list[dict] = []

    def issue(target: list, code: str, message: str, **details: Any) -> None:
        target.append({"code": code, "message": message, **details})

    result: dict = {
        "schema_version": 1,
        "generated_at": _iso(datetime.now(UTC)),
        "as_of": None,
        "source_captured_at": None,
        "source_last_success": None,
        "timezone": "Europe/London",
        "currency": "GBP",
        "unit": "p/kWh",
        "price_encoding": "decimal_string",
        "valid": False,
        "ready_for_upload": False,
        "errors": errors,
        "warnings": warnings,
        "upload_blockers": blockers,
        "periods": [],
        "coverage": {},
        "tesla_payload": None,
    }
    if not isinstance(source, dict):
        issue(errors, "invalid_source", "The source must be an object.")
        return result
    try:
        clock = _instant(now if now is not None else source.get("now", datetime.now(UTC)))
    except (ValueError, TypeError, OverflowError):
        issue(errors, "invalid_now", "The assessment time must include a timezone.")
        return result
    result["as_of"] = _iso(clock)
    conversion_known = source.get("unit") == "p/kWh" and source.get("currency") == "GBP"
    if source.get("unit") != "p/kWh":
        issue(errors, "unsupported_unit", "Expected HA prices in p/kWh; no unit conversion was guessed.")
    if source.get("currency") != "GBP":
        issue(errors, "unsupported_currency", "Expected GBP prices; no currency conversion was performed.")
    tz_name = source.get("timezone", "Europe/London")
    try:
        if not isinstance(tz_name, str):
            raise ValueError()
        tz = ZoneInfo(tz_name)
        result["timezone"] = tz_name
    except (ValueError, ZoneInfoNotFoundError, TypeError):
        issue(errors, "invalid_timezone", "The site timezone must be a known IANA timezone.")
        tz = ZoneInfo("Europe/London")
    if tz_name != "Europe/London":
        issue(errors, "unsupported_site_timezone", "This preview currently targets the Europe/London site timezone.")
    for key, output_key in [("captured_at", "source_captured_at"), ("last_success", "source_last_success")]:
        try:
            timestamp = _instant(source.get(key))
            result[output_key] = _iso(timestamp)
        except (ValueError, TypeError, OverflowError):
            issue(errors, "invalid_" + key, "The source timestamp must include a timezone.")
            continue
        age = (clock - timestamp).total_seconds()
        if age < -30:
            issue(errors, key + "_in_future", "The source timestamp is in the future.")
        if age >= FRESHNESS_SECONDS:
            issue(errors, "stale_" + key, "The source is at least 15 minutes old.", age_seconds=round(age, 3))

    channels = source.get("channels")
    if not isinstance(channels, dict):
        issue(errors, "invalid_channels", "Both import and export price channels are required.")
        return result
    parsed: dict[str, list[dict]] = {}
    provisional = False
    forecast = False
    for channel in ("import", "export"):
        rows = channels.get(channel)
        parsed[channel] = []
        if not isinstance(rows, list) or not rows:
            issue(errors, "missing_channel", "The price channel has no periods.", channel=channel)
            continue
        if len(rows) > MAX_PERIODS:
            issue(errors, "too_many_periods", "The price input exceeds the bounded preview limit.", channel=channel)
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                issue(errors, "invalid_period", "Each price period must be an object.", channel=channel, period_index=index)
                continue
            try:
                start, end = _instant(row.get("valid_from")), _instant(row.get("valid_to"))
            except (ValueError, TypeError, OverflowError):
                issue(errors, "invalid_interval", "Both interval endpoints must be timezone-aware timestamps.", channel=channel, period_index=index)
                continue
            if end <= start:
                issue(errors, "invalid_interval_order", "The interval must end after it starts.", channel=channel, period_index=index)
                continue
            if end - start != HALF_HOUR:
                issue(errors, "unsupported_interval_length", "Expected an explicit 30-minute UTC interval.", channel=channel, period_index=index)
            if start.minute not in (0, 30) or start.second or start.microsecond or end.minute not in (0, 30) or end.second or end.microsecond:
                issue(errors, "interval_not_half_hour_aligned", "Periods must align with half-hour UTC boundaries.", channel=channel, period_index=index)
            try:
                price = _money(row.get("value"))
            except ValueError:
                issue(errors, "invalid_price", "A price is missing, boolean, non-finite or outside the bounded range.", channel=channel, period_index=index)
                continue
            if price < 0:
                issue(errors, "negative_price", "Tesla's documented manual-tariff API clamps negative prices; this preview does not alter them.", channel=channel, period_index=index, valid_from=_iso(start))
            quality = row.get("quality")
            if quality not in {"history", "current", "forecast"}:
                issue(errors, "unknown_price_quality", "The period must declare history, current or forecast quality.", channel=channel, period_index=index)
                quality = None
            estimate = row.get("estimate")
            if not isinstance(estimate, bool):
                issue(errors, "unknown_estimate_flag", "The period must carry an explicit estimate flag.", channel=channel, period_index=index)
                estimate = None
            basis = row.get("interval_basis")
            if basis not in {"provider_end", "provisional_30m"}:
                issue(errors, "unknown_interval_basis", "The interval endpoint provenance is not recognised.", channel=channel, period_index=index)
                basis = None
            provisional |= basis == "provisional_30m"
            forecast |= quality == "forecast" or estimate is True
            parsed[channel].append({"start": start, "end": end, "price": price, "quality": quality, "estimate": estimate, "basis": basis, "index": index})
        parsed[channel].sort(key=lambda item: (item["start"], item["end"]))
        for previous, current in zip(parsed[channel], parsed[channel][1:]):
            if current["start"] < previous["end"]:
                issue(errors, "overlapping_periods", "The channel contains duplicate or overlapping periods.", channel=channel, period_index=current["index"])
            elif current["start"] > previous["end"]:
                issue(errors, "gap_in_periods", "The channel contains a gap; no replacement price was invented.", channel=channel, valid_from=_iso(previous["end"]), valid_to=_iso(current["start"]))
    if provisional:
        issue(warnings, "provisional_interval_ends", "Some endpoints are provisional 30-minute intervals rather than explicit provider ends.")
    if forecast:
        issue(warnings, "forecast_not_settlement", "Forecast prices remain forecasts even when estimate=false; they are not confirmed settlement prices.")

    maps: dict[str, dict] = {}
    for channel, rows in parsed.items():
        maps[channel] = {(r["start"], r["end"]): r for r in rows}
    import_keys, export_keys = set(maps["import"]), set(maps["export"])
    if import_keys != export_keys:
        issue(errors, "unpaired_channels", "Import and export must cover exactly the same intervals.", import_only_count=len(import_keys-export_keys), export_only_count=len(export_keys-import_keys))
    aligned: list[dict] = []
    local_groups: dict[tuple, list[dict]] = defaultdict(list)
    for key in sorted(import_keys & export_keys):
        imp, exp = maps["import"][key], maps["export"][key]
        compatible = imp["price"] >= 0 and exp["price"] >= 0 and imp["price"] >= exp["price"]
        if imp["price"] < exp["price"]:
            issue(errors, "export_exceeds_import", "Tesla's documented manual-tariff API raises the buy price to the sell price; an exact mirror is not possible for this period.", valid_from=_iso(imp["start"]), valid_to=_iso(imp["end"]))
        start_local, end_local = imp["start"].astimezone(tz), imp["end"].astimezone(tz)
        row = {
            "valid_from": _iso(imp["start"]), "valid_to": _iso(imp["end"]),
            "local_from": start_local.isoformat(), "local_to": end_local.isoformat(),
            "import_p_per_kwh": _decimal(imp["price"]), "export_p_per_kwh": _decimal(exp["price"]),
            "import_gbp_per_kwh": _decimal(imp["price"] / 100) if conversion_known else None,
            "export_gbp_per_kwh": _decimal(exp["price"] / 100) if conversion_known else None,
            "import_quality": imp["quality"], "export_quality": exp["quality"],
            "import_estimate": imp["estimate"], "export_estimate": exp["estimate"],
            "import_interval_basis": imp["basis"], "export_interval_basis": exp["basis"],
            "compatible": compatible, "is_current": imp["start"] <= clock < imp["end"],
            "is_future": imp["start"] > clock,
        }
        result["periods"].append(row)
        aligned.append(imp)
        local_groups[(start_local.date(), start_local.hour, start_local.minute)].append({"import": imp["price"], "export": exp["price"], "fold": start_local.fold, "row": row})
    for (day, hour, minute), group in local_groups.items():
        if len(group) <= 1 or len({x["fold"] for x in group}) <= 1:
            continue
        if len({(x["import"], x["export"]) for x in group}) > 1:
            issue(errors, "ambiguous_dst_rates", "Different prices occur at the same repeated local clock time; Tesla's clock-only TOU periods cannot distinguish these folds.", local_time=f"{day.isoformat()}T{hour:02}:{minute:02}")
            for x in group:
                x["row"]["compatible"] = False
        else:
            issue(warnings, "repeated_local_time", "A repeated local clock time has equal prices; explicit DST-aware schedule validation is still required.", local_time=f"{day.isoformat()}T{hour:02}:{minute:02}")

    today = clock.astimezone(tz).date()
    midnight = lambda d: datetime.combine(d, time.min, tzinfo=tz).astimezone(UTC)
    today_start, tomorrow_start, after_tomorrow = midnight(today), midnight(today+timedelta(days=1)), midnight(today+timedelta(days=2))
    full_today, full_tomorrow = _covered(aligned, today_start, tomorrow_start), _covered(aligned, tomorrow_start, after_tomorrow)
    current_present = any(r["start"] <= clock < r["end"] for r in aligned)
    if not current_present:
        issue(errors, "current_period_missing", "The current half-hour has no complete import/export pair.")
    active = [r for r in aligned if r["end"] > clock]
    if not active or max(r["end"] for r in active) <= clock + HALF_HOUR:
        issue(errors, "insufficient_future_coverage", "The source does not cover more than the current interval.")
    local_days = []
    days = sorted({r["start"].astimezone(tz).date() for r in aligned})
    for day in days:
        start, end = midnight(day), midnight(day+timedelta(days=1))
        expected = int((end-start)/HALF_HOUR)
        count = sum(start <= r["start"] < end for r in aligned)
        local_days.append({"date": day.isoformat(), "expected_periods": expected, "observed_periods": count, "complete": _covered(aligned, start, end), "dst_transition": expected != 48})
    year_start, year_end = midnight(date(today.year, 1, 1)), midnight(date(today.year+1, 1, 1))
    full_year = _covered(aligned, year_start, year_end)
    result["coverage"] = {
        "start": _iso(aligned[0]["start"]) if aligned else None,
        "end": _iso(aligned[-1]["end"]) if aligned else None,
        "period_count": len(aligned), "active_period_count": len(active),
        "horizon_hours": round(max(0, (aligned[-1]["end"]-clock).total_seconds()/3600), 6) if aligned else 0,
        "current_period_present": current_present, "full_today": full_today, "full_tomorrow": full_tomorrow,
        "full_calendar_year": full_year, "local_days": local_days,
    }
    if not full_today or not full_tomorrow:
        issue(warnings, "partial_calendar_day_quotes", "The rolling horizon is not a complete quotation for both calendar days. The candidate is a daily clock template, not a historical or full-tomorrow billing record.", full_today=full_today, full_tomorrow=full_tomorrow)

    # Tesla's seasons must cover the calendar structurally. That does not mean
    # a dynamic source needs a year of price quotations: one all-year season
    # can repeat a daily template. The expiry of its *known* prices remains an
    # operational requirement, disclosed separately below.
    window = active[:48]
    template_ready = len(window) == 48
    if template_ready:
        template_ready = all(r["end"]-r["start"] == HALF_HOUR for r in window)
        template_ready &= all(a["end"] == c["start"] for a, c in zip(window, window[1:]))
    clock_rows: dict[tuple[int, int], dict] = {}
    if template_ready:
        for row in window:
            local = row["start"].astimezone(tz)
            key = (local.hour, local.minute)
            if key in clock_rows:
                template_ready = False
                break
            clock_rows[key] = row
        template_ready &= len(clock_rows) == 48
        template_ready &= set(clock_rows) == {(hour, minute) for hour in range(24) for minute in (0, 30)}
    mapping_known = conversion_known and tz_name == "Europe/London"
    if not mapping_known:
        issue(blockers, "payload_units_or_timezone_unverified", "No Tesla payload can be generated by guessing the source unit, currency or site timezone.")
    elif not template_ready:
        issue(blockers, "daily_template_not_representable", "A candidate needs 48 contiguous half-hours with all 48 local clock starts exactly once. Missing data or a DST clock transition prevents constructing that template without inventing or conflating prices.")
    else:
        import_rates: dict[str, float] = {}
        export_rates: dict[str, float] = {}
        tou: dict[str, dict] = {}
        remapped_past = False
        for (hour, minute), row in sorted(clock_rows.items()):
            matching_export = maps["export"][(row["start"], row["end"])]
            label = f"HA_{hour:02}{minute:02}"
            buy = row["price"] / 100
            sell = matching_export["price"] / 100
            buy_number, sell_number = float(buy), float(sell)
            if Decimal(str(buy_number)) != buy or Decimal(str(sell_number)) != sell:
                issue(errors, "payload_decimal_precision", "A price cannot be represented exactly by the proposed JSON number encoding.", valid_from=_iso(row["start"]))
            import_rates[label] = buy_number
            export_rates[label] = sell_number
            end_minute = (hour * 60 + minute + 30) % (24 * 60)
            tou[label] = {"periods": [{"fromDayOfWeek": 0, "toDayOfWeek": 6,
                "fromHour": hour, "fromMinute": minute,
                "toHour": end_minute // 60, "toMinute": end_minute % 60}]}
            remapped_past |= row["start"].astimezone(tz).date() > today
        season = {"fromDay": 1, "toDay": 31, "fromMonth": 1, "toMonth": 12, "tou_periods": tou}
        def side(rates: dict[str, float]) -> dict:
            return {"utility": "E.ON Next", "code": "HA_OPTIMISE_PREVIEW",
                "name": "Next Optimise (HA dry run)", "currency": "GBP",
                "energy_charges": {"HA_DAILY": {"rates": rates}},
                "seasons": {"HA_DAILY": season}}
        payload = side(import_rates)
        payload["version"] = 1
        payload["sell_tariff"] = side(export_rates)
        result["tesla_payload"] = payload
        result["payload_manifest"] = {
            "kind": "non_uploading_daily_template_candidate",
            "known_valid_from": _iso(window[0]["start"]),
            "known_valid_until": _iso(window[-1]["end"]),
            "source_period_count": 48, "source_periods_not_used": max(0, len(active)-48),
            "repeats_daily": True, "annual_season_is_structure_not_annual_price_forecast": True,
            "historical_clock_prices_may_be_remapped": remapped_past,
            "negative_or_crossed_prices_preserved": True,
            "clamping_performed": False,
            "upload_performed": False,
        }
        issue(warnings, "daily_template_repeats_beyond_known_horizon", "The all-year season repeats these daily prices beyond their source horizon. Those later repetitions are not genuine quotations; a future updater needs a tested expiry/fallback policy.")
        if remapped_past:
            issue(warnings, "historical_clock_prices_remapped", "Some clock labels use the following day's known prices. A repeated daily template can therefore alter the apparent prices for elapsed hours; it is not suitable as a historical billing record.")
        issue(warnings, "custom_period_labels", "The API permits arbitrary labels, but the Tesla app only documents named display support for four standard period labels. The 48-label candidate needs visual readback verification.")
    issue(blockers, "expiry_policy_unapproved", "A live updater's expiry, stale-source and fallback policy must be agreed and tested before enabling uploads.")
    issue(blockers, "control_and_write_path_unverified", "Battery-controller coordination, write authorisation and Tesla readback have not been verified by this pure dry run.")
    result["valid"] = not errors
    result["candidate_payload_generated"] = result["tesla_payload"] is not None
    result["candidate_payload_valid"] = result["valid"] and result["candidate_payload_generated"]
    result["summary"] = {
        "paired_periods": len(result["periods"]),
        "incompatible_periods": sum(not p["compatible"] for p in result["periods"]),
        "error_count": len(errors), "warning_count": len(warnings), "upload_blocker_count": len(blockers),
        "credentials_needed": False, "network_requests": 0, "tesla_writes": 0,
    }
    return result
