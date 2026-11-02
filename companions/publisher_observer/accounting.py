"""Pure, conservative accounting for the read-only Powerwall observation.

Inputs contain AC energy in kWh and tariff quotes in p/kWh.  This module performs
no I/O and sends no controls.  A quote's presence means the caller has already
validated its actual validity interval: this module never extends that interval.
All costs are for the measured trace, excluding standing charges and settlement
adjustments.  A Self-Powered result is an illustrative counterfactual, never an
observed saving attributable to the controller.
"""
from __future__ import annotations

import math


ENERGY_FIELDS = (
    "grid_import_kwh", "grid_export_kwh", "battery_charge_kwh",
    "battery_discharge_kwh", "solar_kwh", "home_kwh",
)
EPSILON = 1e-10


def _number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _bounds(start, end, now=None):
    if not _number(start) or not _number(end) or end <= start:
        raise ValueError("window_requires_finite_increasing_epoch_seconds")
    if now is not None and not _number(now):
        raise ValueError("now_requires_finite_epoch_seconds")
    return float(start), float(end if now is None else max(start, min(end, now)))


def _prepare(segments, start, end):
    """Clip and de-duplicate a trace, keeping the earliest sorted observation.

    Energy is apportioned by duration when clipped. The caller should split
    tariff boundaries first. Overlap is exposed rather than double-counted;
    ambiguous overlap makes the optional model ineligible.
    """
    valid = []
    invalid = 0
    for position, row in enumerate(segments):
        if (not isinstance(row, dict) or not _number(row.get("start"))
                or not _number(row.get("end")) or row["end"] <= row["start"]
                or any(not _number(row.get(key)) or row[key] < 0 for key in ENERGY_FIELDS)):
            invalid += 1
            continue
        if row["end"] > start and row["start"] < end:
            valid.append((float(row["start"]), float(row["end"]), position, row))
    valid.sort(key=lambda item: item[:3])
    prepared = []
    cursor = start
    overlap_seconds = 0.0
    for raw_start, raw_end, _, row in valid:
        left, right = max(start, raw_start), min(end, raw_end)
        overlap_seconds += max(0.0, min(right, cursor) - left)
        left = max(left, cursor)
        if right <= left:
            continue
        scale = (right - left) / (raw_end - raw_start)
        clipped = {key: float(row[key]) * scale for key in ENERGY_FIELDS}
        clipped.update(start=left, end=right, quality=row.get("quality", "unverified"))
        for prefix in ("optimise", "agile"):
            for direction in ("import", "export"):
                key = f"{prefix}_{direction}_p"
                value = row.get(key)
                clipped[key] = float(value) if _number(value) else None
        prepared.append(clipped)
        cursor = right
    return prepared, invalid, overlap_seconds


def _percent(seconds, expected_seconds):
    # An empty future window has no observed coverage, rather than an invented 100%.
    return min(100.0, 100.0 * seconds / expected_seconds) if expected_seconds else 0.0


def _costs(rows, tariff):
    imported = exported = unpriced_import = unpriced_export = priced_seconds = 0.0
    for row in rows:
        import_price, export_price = row[f"{tariff}_import_p"], row[f"{tariff}_export_p"]
        if import_price is None:
            unpriced_import += row["grid_import_kwh"]
        else:
            imported += row["grid_import_kwh"] * import_price / 100.0
        if export_price is None:
            unpriced_export += row["grid_export_kwh"]
        else:
            exported += row["grid_export_kwh"] * export_price / 100.0
        if import_price is not None and export_price is not None:
            priced_seconds += row["end"] - row["start"]
    return {
        "import_cost_gbp": imported, "export_revenue_gbp": exported,
        "net_cost_gbp": imported - exported,
        "unpriced_import_kwh": unpriced_import, "unpriced_export_kwh": unpriced_export,
        "priced_seconds": priced_seconds,
    }


def summarize_window(segments, start, end, now=None):
    """Summarize the measured part of [start, min(end, now)).

    With ``now=None`` the whole requested window is assessed. Costs remain
    numeric partial totals when prices or samples are missing; coverage and the
    partial flags must accompany them. Missing energy is unknown, not zero.
    """
    start, effective_end = _bounds(start, end, now)
    rows, invalid, overlap = _prepare(segments, start, effective_end)
    expected = effective_end - start
    covered = sum(row["end"] - row["start"] for row in rows)
    monitored = sum(row["end"] - row["start"] for row in rows if row["quality"] == "monitored")
    result = {key: sum(row[key] for row in rows) for key in ENERGY_FIELDS}
    result.update(
        start=start, end=float(end), effective_end=effective_end,
        expected_hours=expected / 3600.0, covered_hours=covered / 3600.0,
        coverage_percent=_percent(covered, expected),
        measured_coverage_percent=_percent(covered, expected),
        monitored_hours=monitored / 3600.0,
        monitored_coverage_percent=_percent(monitored, expected),
        unmeasured_hours=max(0.0, expected - covered) / 3600.0,
        invalid_segment_count=invalid, overlap_hours_ignored=overlap / 3600.0,
        segment_count=len(rows),
        energy_source="caller_supplied_directional_AC_energy; gaps_are_not_filled",
        comparison_basis="same_observed_power_flows_repriced; not_controller_savings",
    )
    for tariff, prefix in (("optimise", ""), ("agile", "same_trace_agile_")):
        costs = _costs(rows, tariff)
        priced = costs.pop("priced_seconds")
        result.update({prefix + key: value for key, value in costs.items()})
        result[prefix + "priced_coverage_percent"] = _percent(priced, expected)
        result[prefix + "priced_covered_hours"] = priced / 3600.0
        result[prefix + "unpriced_hours"] = max(0.0, covered - priced) / 3600.0
        result[prefix + "net_cost_is_partial"] = (expected <= 0 or priced < expected - EPSILON)
    result["same_trace_agile_minus_optimise_net_cost_gbp"] = (
        result["same_trace_agile_net_cost_gbp"] - result["net_cost_gbp"]
        if not result["net_cost_is_partial"] and not result["same_trace_agile_net_cost_is_partial"]
        else None
    )
    return result


def simulate_self_powered(
    segments, start, end, initial_stored_kwh, capacity_kwh,
    reserve_floor_kwh, roundtrip_efficiency, max_power_kw,
    actual_final_stored_kwh=None,
):
    """Illustrate Self-Powered with fixed observed load/solar and explicit physics.

    Caller must first rule out manual/reserve changes, outages and E.ON battery
    enrolment. No model is produced below 99% measured, paired-price or monitored
    time coverage. Missing intervals stay unmodelled and inventory holds through
    gaps; even an eligible 99%-covered model can have partial cash totals.

    Storage is on the caller's native remaining-energy basis. Reserve floor and
    capacity must use the same basis. Roundtrip efficiency is split symmetrically
    between charge and discharge. Native inventory freshness is not independently
    verified here. Terminal values are illustrative scenarios, not future quotes.
    """
    start, end = _bounds(start, end)
    parameters = (initial_stored_kwh, capacity_kwh, reserve_floor_kwh,
                  roundtrip_efficiency, max_power_kw)
    if (not all(_number(value) for value in parameters) or capacity_kwh <= 0
            or not 0 <= initial_stored_kwh <= capacity_kwh
            or not 0 <= reserve_floor_kwh <= capacity_kwh
            or not 0 < roundtrip_efficiency <= 1 or max_power_kw <= 0
            or (actual_final_stored_kwh is not None
                and (not _number(actual_final_stored_kwh) or actual_final_stored_kwh < 0))):
        raise ValueError("invalid_self_powered_physical_parameters")
    rows_input = list(segments)
    summary = summarize_window(rows_input, start, end)
    reasons = [key + "_below_99_percent" for key in (
        "coverage_percent", "priced_coverage_percent", "monitored_coverage_percent"
    ) if summary[key] < 99.0 - EPSILON]
    if summary["invalid_segment_count"]:
        reasons.append("invalid_segments_present")
    if summary["overlap_hours_ignored"] > EPSILON:
        reasons.append("overlapping_segments_present")
    result = {
        "eligible": not reasons, "reason_codes": reasons, "illustrative": True,
        "claim": "model_counterfactual_only; no_definitive_controller_savings_claim",
        "native_inventory_freshness_verified": False,
        "summary": summary,
        "assumptions": {
            "initial_stored_kwh": initial_stored_kwh, "capacity_kwh": capacity_kwh,
            "reserve_floor_kwh": reserve_floor_kwh,
            "roundtrip_efficiency": roundtrip_efficiency, "max_power_kw": max_power_kw,
            "same_observed_household_EV_load_and_solar": True,
            "grid_charging": False, "deliberate_battery_export": False,
            "charge_discharge_efficiency_split": "sqrt_roundtrip_each_way",
            "missing_intervals": "unmodelled; inventory_held_constant",
            "native_inventory": "caller_supplied; device_poll_freshness_unverified",
            "reserve_mapping": "caller_scenario; displayed_to_raw_mapping_not_verified",
            "terminal_values": "0/20/65p per stored kWh; illustrative_not_future_quotes",
            "external_conditions": "caller_must_exclude_manual_reserve_outage_enrolment_changes",
        },
    }
    if reasons:
        return result
    rows, _, _ = _prepare(rows_input, start, end)
    efficiency = math.sqrt(roundtrip_efficiency)
    stored = float(initial_stored_kwh)
    model_rows = []
    for row in rows:
        net_demand = row["home_kwh"] - row["solar_kwh"]
        power_limit = max_power_kw * (row["end"] - row["start"]) / 3600.0
        charging = discharging = importing = exporting = 0.0
        if net_demand >= 0:
            discharging = min(net_demand, power_limit, max(0.0, stored - reserve_floor_kwh) * efficiency)
            stored -= discharging / efficiency
            importing = max(0.0, net_demand - discharging)
        else:
            surplus = -net_demand
            charging = min(surplus, power_limit, max(0.0, capacity_kwh - stored) / efficiency)
            stored += charging * efficiency
            exporting = max(0.0, surplus - charging)
        model_rows.append(dict(row, grid_import_kwh=importing, grid_export_kwh=exporting,
                               battery_charge_kwh=charging, battery_discharge_kwh=discharging))
    model = summarize_window(model_rows, start, end)
    cash_difference = model["net_cost_gbp"] - summary["net_cost_gbp"]
    inventory_delta = None if actual_final_stored_kwh is None else actual_final_stored_kwh - stored
    estimates = [] if inventory_delta is None else [
        {"terminal_value_p_per_stored_kwh": value,
         "inventory_adjustment_gbp": inventory_delta * value / 100.0,
         "cash_difference_gbp": cash_difference + inventory_delta * value / 100.0}
        for value in (0, 20, 65)
    ]
    result.update(
        model=model, model_ending_stored_kwh=stored, model_ending_stored_wh=stored * 1000.0,
        actual_ending_stored_kwh=actual_final_stored_kwh,
        actual_ending_stored_wh=None if actual_final_stored_kwh is None else actual_final_stored_kwh * 1000.0,
        ending_inventory_delta_kwh=inventory_delta,
        ending_inventory_delta_wh=None if inventory_delta is None else inventory_delta * 1000.0,
        unadjusted_cash_difference_gbp=cash_difference,
        monetary_estimates_are_partial=summary["net_cost_is_partial"],
        adjusted_estimates=estimates,
        adjusted_range_gbp=None if not estimates else {
            "min": min(row["cash_difference_gbp"] for row in estimates),
            "max": max(row["cash_difference_gbp"] for row in estimates),
        },
        difference_sign="positive_means_actual_cash_cost_is_lower_than_model",
        inventory_delta_sign="actual_ending_minus_model_ending_stored_energy",
    )
    return result
