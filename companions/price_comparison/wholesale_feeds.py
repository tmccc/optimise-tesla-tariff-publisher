"""Read-only, bounded official GB wholesale price feeds; Python stdlib only.

NESO's ``Date`` and ``Delivery Period (GMT)`` fields describe UTC delivery
intervals, including in summer. N2EX is hourly day-ahead, not the EPEX 30-minute
day-ahead auction used by Agile. Elexon APXMIDP is a short-term market index,
not a day-ahead auction forecast. Neither series includes retail VAT or fees.

Official references:
https://www.neso.energy/data-portal/day-ahead-power-exchange-prices-nordpool
https://www.neso.energy/data-portal/day-ahead-power-exchange-prices-nordpool/_n2ex_gb_day-ahead_price
https://data.elexon.co.uk/swagger/v1/swagger.json
https://bscdocs.elexon.co.uk/bsc-module/category-3-documents/market-index-definition-statement

Fetch functions return {source_id, source_name, market, fetched_at, url,
intervals, coverage}. Interval timestamps are UTC; prices are p/kWh. A failed
request raises: callers should retain their cached data and expose its age.
No future interval is fabricated and missing prices remain absent.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


UTC = timezone.utc
LONDON = ZoneInfo("Europe/London")
NESO_RESOURCE_ID = "4f27eea5-7038-4f73-9740-e3e4ad47c26a"
NESO_API = "https://api.neso.energy/api/3/action/datastore_search"
ELEXON_API = "https://data.elexon.co.uk/bmrs/api/v1/balancing/pricing/market-index"
USER_AGENT = "home-assistant-price-comparison/1.0"
MAX_RESPONSE_BYTES = 1_000_000


def _utc_now(now: datetime | None) -> datetime:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Feed timestamp has no timezone")
    return parsed.astimezone(UTC)


def _number(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (ValueError, InvalidOperation):
        return None
    return number if number.is_finite() else None


def _get_json(url: str, timeout: float) -> dict:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        payload = response.read(MAX_RESPONSE_BYTES + 1)
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ValueError("Wholesale response exceeds bounded payload size")
    result = json.loads(payload)
    if not isinstance(result, dict):
        raise ValueError("Expected a JSON object")
    return result


def _prices(price: Decimal) -> dict:
    # 1 GBP/MWh = 100 p / 1000 kWh = 0.1 p/kWh.
    return {"price_gbp_per_mwh": float(price), "price_p_per_kwh": float(price / 10)}


def parse_neso_n2ex(records: list[dict]) -> list[dict]:
    """Pure parser of NESO CKAN records; null/invalid prices are omitted.

    Keep real zero and negative auction prices. ``23:00 - 00:00`` ends on the
    next UTC date. Sorting by _id makes the newest record win if a provider
    republishes a timestamp. Malformed delivery timestamps raise deliberately.
    """
    intervals: dict[str, dict] = {}
    for row in sorted(records, key=lambda item: int(item.get("_id", 0))):
        price = _number(row.get("Price"))
        if price is None:
            continue
        day = date.fromisoformat(row["Date"])
        match = re.fullmatch(r"(\d{2}):(\d{2})\s*-\s*(\d{2}):(\d{2})", row["Delivery Period"].strip())
        if match is None:
            raise ValueError("Unrecognised NESO delivery period")
        hour, minute, end_hour, end_minute = map(int, match.groups())
        start = datetime.combine(day, time(hour, minute), UTC)
        end = start + timedelta(hours=1)
        if minute != 0 or (end.hour, end.minute) != (end_hour, end_minute):
            raise ValueError("Expected one-hour NESO UTC delivery period")
        key = _iso(start)
        intervals[key] = {"start": key, "end": _iso(end), **_prices(price)}
    return [intervals[key] for key in sorted(intervals)]


def parse_elexon_mid(records: list[dict], provider: str = "APXMIDP") -> list[dict]:
    """Pure parser of Elexon MID rows; exclude zero-volume defaults.

    Zero price with positive volume is valid. Zero volume indicates an index
    absent/below its liquidity threshold, not free electricity. Use supplied
    UTC startTime, never infer UTC from settlementPeriod * 30 minutes.
    """
    intervals: dict[str, dict] = {}
    for row in records:
        if row.get("dataProvider") != provider:
            continue
        price, volume = _number(row.get("price")), _number(row.get("volume"))
        if price is None or volume is None or volume <= 0:
            continue
        start = _parse_utc(row["startTime"])
        if start.minute not in (0, 30) or start.second or start.microsecond:
            raise ValueError("Expected a half-hour Elexon delivery start")
        key = _iso(start)
        intervals[key] = {
            "start": key,
            "end": _iso(start + timedelta(minutes=30)),
            **_prices(price),
            "volume_mwh": float(volume),
            "provider": provider,
            "settlement_date": row["settlementDate"],
            "settlement_period": int(row["settlementPeriod"]),
        }
    return [intervals[key] for key in sorted(intervals)]


def coverage(intervals: list[dict], now: datetime | None = None) -> dict:
    """Describe data coverage separately from fetch success/freshness."""
    now = _utc_now(now)
    return {
        "count": len(intervals),
        "earliest_start": intervals[0]["start"] if intervals else None,
        "latest_end": intervals[-1]["end"] if intervals else None,
        "has_current_interval": any(_parse_utc(r["start"]) <= now < _parse_utc(r["end"]) for r in intervals),
        "has_future_intervals": any(_parse_utc(r["start"]) > now for r in intervals),
    }


def fetch_neso_n2ex(now: datetime | None = None, timeout: float = 20) -> dict:
    """Fetch only today's UTC date minus two days through tomorrow.

    Four explicit date filters and a hard limit of 200 records prevent an
    unbounded historical download. The current resource has 24 UTC hours per
    complete UTC date. Tomorrow may end before midnight UTC; leave that gap.
    """
    now = _utc_now(now)
    days = [(now.date() + timedelta(days=offset)).isoformat() for offset in (-2, -1, 0, 1)]
    url = NESO_API + "?" + urlencode({
        "resource_id": NESO_RESOURCE_ID,
        "filters": json.dumps({"Date": days}, separators=(",", ":")),
        "sort": "Date asc,Delivery Period asc",
        "limit": 200,
    })
    payload = _get_json(url, timeout)
    if payload.get("success") is not True:
        raise ValueError("NESO CKAN reported an unsuccessful request")
    result = payload["result"]
    rows = result["records"]
    if int(result.get("total", len(rows))) > len(rows):
        raise ValueError("NESO bounded response was truncated")
    if any(row.get("Date") not in days for row in rows):
        raise ValueError("NESO returned records outside requested dates")
    intervals = parse_neso_n2ex(rows)
    if not intervals:
        raise ValueError("NESO returned no usable prices")
    return {
        "source_id": "neso_n2ex_day_ahead",
        "source_name": "NESO / Nord Pool N2EX",
        "market": "GB hourly day-ahead auction",
        "fetched_at": _iso(datetime.now(UTC)),
        "url": url,
        "intervals": intervals,
        "coverage": coverage(intervals, now),
    }


def fetch_elexon_mid(now: datetime | None = None, timeout: float = 20) -> dict:
    """Fetch at most 72 hours of APXMIDP, including current period if present."""
    now = _utc_now(now)
    start = now - timedelta(days=3)
    url = ELEXON_API + "?" + urlencode({
        "from": _iso(start), "to": _iso(now), "dataProviders": "APXMIDP", "format": "json",
    })
    payload = _get_json(url, timeout)
    intervals = parse_elexon_mid(payload["data"])
    intervals = [row for row in intervals if start <= _parse_utc(row["start"]) <= now]
    if not intervals:
        raise ValueError("Elexon returned no usable prices")
    return {
        "source_id": "elexon_apx_mid",
        "source_name": "Elexon / APXMIDP",
        "market": "GB half-hour short-term market index",
        "fetched_at": _iso(datetime.now(UTC)),
        "url": url,
        "intervals": intervals,
        "coverage": coverage(intervals, now),
    }


def london_day_intervals(intervals: list[dict], day: date) -> list[dict]:
    """Select actual intervals in a London calendar day, preserving DST.

    Fully covered London days contain 23/24/25 hourly N2EX intervals or
    46/48/50 half-hour MID intervals. Labels alone can repeat in October;
    retain UTC timestamps as keys and display a timezone/offset when needed.
    """
    start = datetime.combine(day, time.min, LONDON).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, LONDON).astimezone(UTC)
    return [row for row in intervals if start <= _parse_utc(row["start"]) < end]


def current_interval(intervals: list[dict], now: datetime | None = None) -> dict | None:
    """Do not carry a previous price into an absent current delivery period."""
    now = _utc_now(now)
    return next((row for row in intervals if _parse_utc(row["start"]) <= now < _parse_utc(row["end"])), None)


def cache_is_fresh(feed: dict, now: datetime | None = None, max_age_minutes: int = 90) -> bool:
    """UI freshness policy, separate from the age of a delivery interval."""
    now = _utc_now(now)
    age = (now - _parse_utc(feed["fetched_at"])).total_seconds()
    return 0 <= age <= max_age_minutes * 60
