"""Analysis engine.

Estimands
---------
``assigned``            raw audit view: every unit with an assignment inside the
                        version window, no exclusions. Used to price every filter.
``intention_to_treat``  every eligible unit, arm = **initial** assignment. All of
                        the unit's outcomes stay in that arm, so switching and
                        cross-device duplicates never move outcomes between arms.
``as_treated``          only units with an exposure event; each outcome is
                        attributed to the arm of the assignment that emitted it.
                        A unit exposed in both arms contributes to both.
``per_protocol``        units with a single consistent arm matching the initial
                        assignment and at least one exposure.

Point-in-time analysis
----------------------
A version can be analysed *as of* its close time (``as_of``): events received
after that instant are excluded, reproducing the frozen snapshot even though
the warehouse keeps accepting late results. ``as_of=None`` is the current view
and is what snapshot verification compares against.

Every filter decision is recorded with a rule name and a human readable reason.
Nothing is dropped silently.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime
from typing import Iterable

from . import stats
from .allocation import allocate, stratum_key

POPULATION_LABELS = {
    "assigned": "Raw assigned (audit)",
    "intention_to_treat": "Intention to treat (ITT)",
    "as_treated": "Actual exposure (as-treated)",
    "per_protocol": "Per protocol",
}

POPULATION_DEFINITIONS = {
    "assigned": "Every unit with an assignment inside the version window, with no exclusions. Used to audit the cost of every filter.",
    "intention_to_treat": "Every eligible unit, arm fixed by the initial assignment. Outcomes include all of the unit's events regardless of later switching.",
    "as_treated": "Only units with an exposure event; each outcome is attributed to the arm of the assignment that emitted it. Units may appear in both arms.",
    "per_protocol": "Units with a single consistent arm matching the initial assignment and at least one exposure. Crossovers are removed.",
}

EXPOSURE_EVENT_DEFAULT = "exposure"

EXCLUSION_REASONS = {
    "external_contamination": "Flagged by an external contamination list",
    "forced_assignment": "Force-assigned, not randomised",
    "variant_conflict": "Seen in multiple arms while switching is disallowed",
    "pre_exposure_outcome": "Outcome recorded before the first assignment",
    "event_before_assignment": "Event occurred before its assignment",
    "assignment_outside_window": "Event belongs to an assignment outside this version",
    "duplicate_event": "Identical payload replayed under a new event id",
    "metric_definition_changed": "Recorded under an outdated metric definition",
    "outside_version_window": "Occurred outside the version window",
    "truncated_window": "Assigned before the intentional truncation point",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _sha(items: Iterable[str]) -> str:
    h = hashlib.sha256()
    for item in sorted(items):
        h.update(item.encode())
        h.update(b"\n")
    return h.hexdigest()


def _in_window(ts: str | None, start: str | None, end: str | None) -> bool:
    if ts is None:
        return False
    if start is not None and ts < start:
        return False
    if end is not None and ts >= end:
        return False
    return True


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _aggregate(metric: dict, events: list[dict]) -> float | None:
    """Collapse one unit's events for one metric into a single value."""
    if not events:
        return None
    values = [e["value"] for e in events]
    if metric["type"] == "proportion":
        return 1.0 if any(v >= 1.0 for v in values) else 0.0
    aggregation = metric.get("aggregation", "sum")
    if aggregation == "mean":
        return sum(values) / len(values)
    if aggregation == "max":
        return max(values)
    if aggregation == "min":
        return min(values)
    return sum(values)


class _Unit:
    __slots__ = ("user_id", "assignments", "exposure_events", "outcome_events", "devices")

    def __init__(self, user_id: str):
        self.user_id = user_id
        self.assignments: list[dict] = []
        self.exposure_events: list[dict] = []
        self.outcome_events: list[dict] = []
        self.devices: set[str] = set()

    @property
    def variants(self) -> list[str]:
        return sorted({a["variant"] for a in self.assignments})

    @property
    def initial(self) -> dict:
        return min(self.assignments, key=lambda a: (a["assigned_at"], a["assignment_id"]))

    @property
    def initial_variant(self) -> str:
        return self.initial["variant"]

    @property
    def switched(self) -> bool:
        return len(self.variants) > 1

    def assignment_arm(self, assignment_id: str) -> str | None:
        for a in self.assignments:
            if a["assignment_id"] == assignment_id:
                return a["variant"]
        return None


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------
def analyze(exp, version, assignments: list[dict], events: list[dict], *, as_of: str | None = None) -> dict:
    cfg = version.config or {}
    start = version.window_start or version.effective_from
    end = version.window_end
    exposure_type = cfg.get("exposure_event_type", EXPOSURE_EVENT_DEFAULT)
    rules = cfg.get("rules", {})
    forced = rules.get("force_assignments", {})
    external_ids = {str(x) for x in rules.get("contaminated_external_ids", [])}
    external_reasons = rules.get("contaminated_reasons", {})
    truncate_before = rules.get("truncate_assigned_before")
    metric_versions = {
        k: str(v) for k, v in (cfg.get("metric_versions") or {}).items()
    }
    if not metric_versions:
        metric_versions = {m["key"]: str(m.get("version", "1")) for m in exp.metrics}

    assignment_by_id = {a["assignment_id"]: a for a in assignments}

    # -- 1. assignments inside the version window -------------------------
    in_window: list[dict] = []
    outside_window_assignments = 0
    truncated_assignments = 0
    for a in assignments:
        if not _in_window(a["assigned_at"], start, end):
            outside_window_assignments += 1
            continue
        if truncate_before and a["assigned_at"] < truncate_before:
            truncated_assignments += 1
            continue
        in_window.append(a)

    unit_map: dict[str, _Unit] = {}
    for a in in_window:
        unit = unit_map.setdefault(a["user_id"], _Unit(a["user_id"]))
        unit.assignments.append(a)
        if a.get("device_id"):
            unit.devices.add(a["device_id"])

    # -- 2. events --------------------------------------------------------
    event_exclusions: list[dict] = []
    seen_signature: dict[tuple, str] = {}
    included_events: list[dict] = []
    orphan_events = 0
    outside_window_events = 0
    pre_assignment_events = 0
    duplicate_events = 0
    stale_metric_events = 0
    late_received: list[dict] = []
    late_occurred: list[dict] = []

    for e in events:
        assignment = assignment_by_id.get(e["assignment_id"])
        if assignment is None:
            orphan_events += 1
            continue
        if not _in_window(assignment["assigned_at"], start, end):
            outside_window_events += 1
            event_exclusions.append(
                {
                    "assignment_id": e["assignment_id"],
                    "user_id": assignment["user_id"],
                    "rule": "assignment_outside_window",
                    "reason": EXCLUSION_REASONS["assignment_outside_window"],
                    "detail": {"event_id": e["event_id"], "assigned_at": assignment["assigned_at"]},
                }
            )
            continue
        if e["occurred_at"] < assignment["assigned_at"]:
            pre_assignment_events += 1
            event_exclusions.append(
                {
                    "assignment_id": e["assignment_id"],
                    "user_id": assignment["user_id"],
                    "rule": "event_before_assignment",
                    "reason": EXCLUSION_REASONS["event_before_assignment"],
                    "detail": {
                        "event_id": e["event_id"],
                        "occurred_at": e["occurred_at"],
                        "assigned_at": assignment["assigned_at"],
                    },
                }
            )
            continue
        if not _in_window(e["occurred_at"], start, end):
            outside_window_events += 1
            if e.get("occurred_at") and (end is None or e["occurred_at"] >= end):
                late_occurred.append(_late_record(e, "occurred_after_close"))
            event_exclusions.append(
                {
                    "assignment_id": e["assignment_id"],
                    "user_id": assignment["user_id"],
                    "rule": "outside_version_window",
                    "reason": EXCLUSION_REASONS["outside_version_window"],
                    "detail": {"event_id": e["event_id"], "occurred_at": e["occurred_at"]},
                }
            )
            continue
        if as_of is not None and e.get("received_at") and e["received_at"] > as_of:
            late_received.append(_late_record(e, "received_after_close"))
            event_exclusions.append(
                {
                    "assignment_id": e["assignment_id"],
                    "user_id": assignment["user_id"],
                    "rule": "outside_version_window",
                    "reason": "received after the version closed; excluded from the frozen sample",
                    "detail": {
                        "event_id": e["event_id"],
                        "received_at": e["received_at"],
                        "as_of": as_of,
                    },
                }
            )
            continue
        signature = (
            e["assignment_id"],
            e["event_type"],
            e["metric_key"],
            round(float(e["value"]), 9),
            e["occurred_at"],
        )
        if signature in seen_signature:
            duplicate_events += 1
            event_exclusions.append(
                {
                    "assignment_id": e["assignment_id"],
                    "user_id": assignment["user_id"],
                    "rule": "duplicate_event",
                    "reason": EXCLUSION_REASONS["duplicate_event"],
                    "detail": {"event_id": e["event_id"], "duplicate_of": seen_signature[signature]},
                }
            )
            continue
        seen_signature[signature] = e["event_id"]

        # Events recorded before metric versioning carry no version tag; they
        # belong to the original definition, "1".
        event_metric_version = (e.get("properties") or {}).get("metric_version", "1")
        current_metric_version = metric_versions.get(e["metric_key"])
        if (
            e["event_type"] != exposure_type
            and current_metric_version is not None
            and str(event_metric_version) != current_metric_version
        ):
            stale_metric_events += 1
            event_exclusions.append(
                {
                    "assignment_id": e["assignment_id"],
                    "user_id": assignment["user_id"],
                    "rule": "metric_definition_changed",
                    "reason": (
                        f"recorded under metric_version={event_metric_version}, "
                        f"analysis expects {current_metric_version}"
                    ),
                    "detail": {
                        "event_id": e["event_id"],
                        "metric_key": e["metric_key"],
                        "event_metric_version": str(event_metric_version),
                        "current_metric_version": current_metric_version,
                    },
                }
            )
            continue

        included_events.append(e)
        unit = unit_map.get(assignment["user_id"])
        if unit is None:
            continue
        if e["event_type"] == exposure_type:
            unit.exposure_events.append(e)
        else:
            unit.outcome_events.append(e)

    # -- 3. unit-level contamination --------------------------------------
    unit_exclusions: dict[str, list[dict]] = defaultdict(list)
    for unit in unit_map.values():
        uid = unit.user_id

        def exclude(rule: str, detail: dict, reason: str | None = None) -> None:
            unit_exclusions[uid].append(
                {"rule": rule, "reason": reason or EXCLUSION_REASONS.get(rule, rule), "detail": detail}
            )

        if uid in external_ids:
            exclude(
                "external_contamination",
                {"user_id": uid},
                external_reasons.get(uid, EXCLUSION_REASONS["external_contamination"]),
            )
        for a in unit.assignments:
            hit = a["assignment_id"] if a["assignment_id"] in external_ids else None
            if hit is None and a.get("device_id") and a["device_id"] in external_ids:
                hit = a["device_id"]
            if hit is not None:
                exclude(
                    "external_contamination",
                    {"assignment_id": a["assignment_id"], "device_id": a.get("device_id"), "matched": hit},
                    external_reasons.get(hit, EXCLUSION_REASONS["external_contamination"]),
                )
                break
        if uid in forced:
            exclude("forced_assignment", {"forced_variant": forced[uid]})
        if unit.switched and not exp.allow_switch:
            exclude(
                "variant_conflict",
                {"variants": unit.variants, "assignments": [a["assignment_id"] for a in unit.assignments]},
            )
        first_assignment = min(a["assigned_at"] for a in unit.assignments)
        pre = [e for e in unit.outcome_events if e["occurred_at"] < first_assignment]
        if pre:
            exclude(
                "pre_exposure_outcome",
                {"event_ids": [e["event_id"] for e in pre], "first_assignment": first_assignment},
            )
        for e in unit.exposure_events + unit.outcome_events:
            if e["metric_key"] in rules.get("changed_metrics", []) and start is not None and e["occurred_at"] < start:
                exclude("metric_definition_changed", {"event_id": e["event_id"]})
                break

    window_units = list(unit_map.values())
    excluded_user_ids = set(unit_exclusions)
    eligible_units = [u for u in window_units if u.user_id not in excluded_user_ids]
    exposed_units = [u for u in eligible_units if u.exposure_events]
    per_protocol_units = [u for u in eligible_units if u.exposure_events and not u.switched]

    exclusion_summary: Counter = Counter()
    for items in unit_exclusions.values():
        for item in items:
            exclusion_summary[item["rule"]] += 1
    for item in event_exclusions:
        exclusion_summary[item["rule"]] += 1

    # -- 4. populations ----------------------------------------------------
    populations = {
        "assigned": _build_population(exp, version, "assigned", window_units, "initial"),
        "intention_to_treat": _build_population(exp, version, "intention_to_treat", eligible_units, "initial"),
        "as_treated": _build_population(exp, version, "as_treated", exposed_units, "event"),
        "per_protocol": _build_population(exp, version, "per_protocol", per_protocol_units, "initial"),
    }

    balance = _balance(exp, version, eligible_units, forced)
    lag_values = _lag_values(included_events)

    result = {
        "experiment": {
            "id": exp.id,
            "name": exp.name,
            "allocation_method": exp.allocation_method,
            "strata_columns": exp.strata_columns,
            "cross_device": exp.cross_device,
            "allow_switch": exp.allow_switch,
            "switch_policy": exp.switch_policy,
        },
        "version": {
            "id": version.id,
            "number": version.number,
            "label": version.label,
            "kind": version.kind,
            "status": version.status,
            "effective_from": start,
            "effective_to": end,
            "as_of": as_of,
            "changed_metrics": sorted(rules.get("changed_metrics", [])),
            "reason": version.reason,
        },
        "window": {"start": start, "end": end, "as_of": as_of},
        "population_definitions": POPULATION_DEFINITIONS,
        "populations": populations,
        "balance": balance,
        "exclusions": {
            "by_rule": dict(sorted(exclusion_summary.items())),
            "unit_level": [
                {
                    "user_id": uid,
                    "rule": item["rule"],
                    "reason": item["reason"],
                    "detail": item["detail"],
                }
                for uid, items in sorted(unit_exclusions.items())
                for item in items
            ],
            "event_level": event_exclusions,
        },
        "attrition": _attrition(window_units, eligible_units, exposed_units, unit_exclusions),
        "funnel": _funnel(
            in_window=in_window,
            window_units=window_units,
            eligible=eligible_units,
            exposed=exposed_units,
            per_protocol=per_protocol_units,
            exclusion_summary=exclusion_summary,
            outside_window_assignments=outside_window_assignments,
            truncated_assignments=truncated_assignments,
            excluded_units=len(unit_exclusions),
        ),
        "late_results": {
            "events_occurred_after_close": len(late_occurred),
            "events_received_after_close": len(late_received),
            "events_outside_window": outside_window_events,
            "lag": _summarise_lag(lag_values),
            "examples": (late_received + late_occurred)[:20],
        },
        "diagnostics": {
            "assignments_total": len(assignments),
            "assignments_in_window": len(in_window),
            "assignments_outside_window": outside_window_assignments,
            "assignments_truncated": truncated_assignments,
            "events_total": len(events),
            "events_in_window": len(included_events),
            "events_outside_window": outside_window_events,
            "orphan_events": orphan_events,
            "duplicate_events": duplicate_events,
            "pre_assignment_events": pre_assignment_events,
            "stale_metric_events": stale_metric_events,
            "units": len(unit_map),
            "units_excluded": len(unit_exclusions),
            "units_multi_assignment": sum(1 for u in window_units if len(u.assignments) > 1),
            "units_cross_variant": sum(1 for u in window_units if u.switched),
            "units_cross_device": sum(1 for u in window_units if len(u.devices) > 1),
            "excluded_units_by_variant": dict(
                Counter(u.initial_variant for u in window_units if u.user_id in excluded_user_ids)
            ),
        },
        "checks": [],
    }
    result["checks"] = _checks(
        exp=exp,
        version=version,
        window_units=window_units,
        multi_units=[u for u in window_units if len(u.assignments) > 1],
        late_received=late_received,
        late_occurred=late_occurred,
        duplicate_events=duplicate_events,
        orphan_events=orphan_events,
        stale_metric_events=stale_metric_events,
        balance=balance,
        lag_values=lag_values,
        unit_exclusions=unit_exclusions,
    )
    result["_included_assignment_ids"] = sorted({a["assignment_id"] for a in in_window})
    result["_included_event_ids"] = sorted({e["event_id"] for e in included_events})
    result["snapshot"] = build_snapshot(result, version, exp, as_of=as_of)
    return result


def _late_record(event: dict, reason: str) -> dict:
    return {
        "event_id": event["event_id"],
        "assignment_id": event["assignment_id"],
        "metric_key": event["metric_key"],
        "occurred_at": event["occurred_at"],
        "received_at": event.get("received_at"),
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Population estimation
# ---------------------------------------------------------------------------
def _build_population(exp, version, population: str, units: list[_Unit], arm_mode: str) -> dict:
    out = {
        "population": population,
        "label": POPULATION_LABELS[population],
        "definition": POPULATION_DEFINITIONS[population],
        "units": {"control": 0, "treatment": 0},
        "metrics": [],
        "notes": [],
    }
    if arm_mode == "event":
        arms: dict[str, list[tuple[_Unit, list[dict]]]] = {"control": [], "treatment": []}
        for unit in units:
            for arm in ("control", "treatment"):
                if not any(unit.assignment_arm(e["assignment_id"]) == arm for e in unit.exposure_events):
                    continue
                outcomes = [e for e in unit.outcome_events if unit.assignment_arm(e["assignment_id"]) == arm]
                arms[arm].append((unit, outcomes))
        crossover = sum(
            1
            for unit in units
            if len({unit.assignment_arm(e["assignment_id"]) for e in unit.exposure_events}) > 1
        )
        if crossover:
            out["notes"].append(
                f"{crossover} units were exposed in both arms and contribute outcomes to both arms."
            )
        arm_units = {arm: len(members) for arm, members in arms.items()}
    else:
        arms = {"control": [], "treatment": []}
        for unit in units:
            arm = unit.initial_variant
            if arm in arms:
                arms[arm].append(unit)
        arm_units = {arm: len(members) for arm, members in arms.items()}

    out["units"] = arm_units
    for metric in exp.metrics:
        if arm_mode == "event":
            control_vals = [v for _, outs in arms["control"] if (v := _outcome(metric, outs)) is not None]
            treat_vals = [v for _, outs in arms["treatment"] if (v := _outcome(metric, outs)) is not None]
        else:
            control_vals = [v for u in arms["control"] if (v := _outcome(metric, u.outcome_events)) is not None]
            treat_vals = [v for u in arms["treatment"] if (v := _outcome(metric, u.outcome_events)) is not None]
        out["metrics"].append(
            _metric_result(metric, version, control_vals, treat_vals, arm_units)
        )
    if population == "assigned":
        out["notes"].append("No exclusions applied: this population prices the filters, it is not an estimate.")
    return out


def _outcome(metric: dict, events: list[dict]) -> float | None:
    relevant = [e for e in events if e["metric_key"] == metric["key"]]
    return _aggregate(metric, relevant)


def _metric_result(metric, version, control_vals, treat_vals, arm_units) -> dict:
    changed = set((version.config or {}).get("rules", {}).get("changed_metrics", []))
    if metric["type"] == "proportion":
        x_c = int(sum(1 for v in control_vals if v >= 1.0))
        x_t = int(sum(1 for v in treat_vals if v >= 1.0))
        # Denominator is the arm size: a unit with no event counts as 0.
        comparison = stats.compare_proportions(x_c, arm_units["control"], x_t, arm_units["treatment"])
        comparison["estimator"] = "two-proportion z-test"
        comparison["aggregation"] = "any"
    else:
        comparison = stats.compare_continuous(control_vals, treat_vals)
        comparison["estimator"] = "Welch t-test"
        comparison["aggregation"] = metric.get("aggregation", "sum")
    comparison["metric"] = metric["key"]
    comparison["name"] = metric.get("name", metric["key"])
    comparison["type"] = metric["type"]
    comparison["metric_version"] = str(metric.get("version", "1"))
    comparison["definition_changed_at_version"] = version.number if metric["key"] in changed else None
    comparison["outcome_units"] = {"control": len(control_vals), "treatment": len(treat_vals)}
    return comparison


# ---------------------------------------------------------------------------
# Balance
# ---------------------------------------------------------------------------
def _balance(exp, version, eligible_units: list[_Unit], forced: dict) -> dict:
    cfg = version.config or {}
    method = exp.allocation_method
    weights = cfg.get("stratified", {}).get("stratum_weights", {}) if method == "stratified" else {}
    default_share = float(
        weights.get("__default__", cfg.get("fixed", {}).get("treatment_share", 0.5))
    )

    strata_counts: dict[str, Counter] = defaultdict(Counter)
    for unit in eligible_units:
        key = stratum_key(unit.initial.get("strata", {}), exp.strata_columns)
        strata_counts[key][unit.initial_variant] += 1

    total_control = sum(c["control"] for c in strata_counts.values())
    total_treat = sum(c["treatment"] for c in strata_counts.values())
    n_total = len(eligible_units)
    expected_share = default_share
    if method == "stratified" and strata_counts and n_total:
        expected_share = (
            sum(sum(c.values()) * float(weights.get(k, default_share)) for k, c in strata_counts.items())
            / n_total
        )
    srm = stats.chi_square_test(
        [total_control, total_treat],
        [n_total * (1 - expected_share), n_total * expected_share],
    )
    srm.update(
        {
            "expected_treatment_share": expected_share,
            "observed_treatment_share": total_treat / n_total if n_total else None,
            "alert": srm["p_value"] < 0.001,
            "n": n_total,
        }
    )

    strata_rows = []
    for key, counts in sorted(strata_counts.items()):
        n = sum(counts.values())
        share = float(weights.get(key, default_share))
        test = stats.chi_square_test(
            [counts["control"], counts["treatment"]], [n * (1 - share), n * share]
        )
        strata_rows.append(
            {
                "stratum": key,
                "control": counts["control"],
                "treatment": counts["treatment"],
                "total": n,
                "observed_treatment_share": counts["treatment"] / n if n else None,
                "expected_treatment_share": share,
                "p_value": test["p_value"],
                "alert": test["p_value"] < 0.001 and n >= 30,
            }
        )

    # Covariate balance over every level of every stratification column,
    # measured on the ITT population (one row per unit, initial assignment).
    # SMD compares the level's prevalence *within each arm*, which is the
    # standard standardized mean difference for a binary indicator.
    covariate_rows = []
    for col in exp.strata_columns:
        levels: dict[str, Counter] = defaultdict(Counter)
        for unit in eligible_units:
            level = unit.initial.get("strata", {}).get(col, "<missing>")
            levels[level][unit.initial_variant] += 1
        n_c_total, n_t_total = total_control, total_treat
        for level, counts in sorted(levels.items()):
            n_c, n_t = counts["control"], counts["treatment"]
            p_c = n_c / n_c_total if n_c_total else 0.0
            p_t = n_t / n_t_total if n_t_total else 0.0
            pooled = (p_c * (1 - p_c) + p_t * (1 - p_t)) / 2.0
            smd = (p_t - p_c) / pooled**0.5 if pooled > 0 else 0.0
            covariate_rows.append(
                {
                    "column": col,
                    "level": level,
                    "control": n_c,
                    "treatment": n_t,
                    "total": n_c + n_t,
                    "share_control": p_c,
                    "share_treatment": p_t,
                    "smd": smd,
                    "p_value": stats.chi_square_test(
                        [n_c, n_t],
                        [(n_c + n_t) * (n_c_total / (n_c_total + n_t_total) if n_c_total + n_t_total else 0.5),
                         (n_c + n_t) * (n_t_total / (n_c_total + n_t_total) if n_c_total + n_t_total else 0.5)],
                    )["p_value"],
                    # A 0.1 SMD flag is only actionable when it is also
                    # unlikely to be hash noise at this sample size.
                    "alert": abs(smd) > 0.1 and n_c + n_t >= 30,
                }
            )

    # Allocation integrity: reproduce the *initial* assignment of each unit
    # with the deterministic splitter. Later records (switches) are product
    # events, not randomisation events, so they are excluded.
    mismatches = []
    checked = 0
    for unit in eligible_units:
        if unit.user_id in forced:
            continue
        a = unit.initial
        checked += 1
        expected_variant = allocate(cfg, method, unit.user_id, a.get("strata", {}), exp.strata_columns)
        if expected_variant != a["variant"]:
            mismatches.append(
                {
                    "user_id": unit.user_id,
                    "assignment_id": a["assignment_id"],
                    "ingested": a["variant"],
                    "recomputed": expected_variant,
                }
            )
    integrity = {
        "checked": checked,
        "mismatches": len(mismatches),
        "mismatch_rate": len(mismatches) / checked if checked else 0.0,
        "examples": mismatches[:10],
        "alert": checked > 0 and len(mismatches) / checked > 0.05,
    }

    return {
        "allocation_method": method,
        "srm": srm,
        "strata": strata_rows,
        "covariates": covariate_rows,
        "allocation_integrity": integrity,
    }


# ---------------------------------------------------------------------------
# Attrition & checks
# ---------------------------------------------------------------------------
def _attrition(window_units, eligible_units, exposed_units, unit_exclusions) -> dict:
    eligible_ids = {u.user_id for u in eligible_units}
    exposed_ids = {u.user_id for u in exposed_units}
    by_arm = {
        "control": {"assigned": 0, "eligible": 0, "exposed": 0, "excluded": 0, "no_exposure": 0},
        "treatment": {"assigned": 0, "eligible": 0, "exposed": 0, "excluded": 0, "no_exposure": 0},
    }
    for unit in window_units:
        arm = unit.initial_variant
        if arm not in by_arm:
            continue
        by_arm[arm]["assigned"] += 1
        if unit.user_id in eligible_ids:
            by_arm[arm]["eligible"] += 1
        else:
            by_arm[arm]["excluded"] += 1
        if unit.user_id in exposed_ids:
            by_arm[arm]["exposed"] += 1
        elif unit.user_id in eligible_ids:
            by_arm[arm]["no_exposure"] += 1
    for arm, row in by_arm.items():
        row["exposure_rate"] = row["exposed"] / row["assigned"] if row["assigned"] else None
        row["attrition_rate"] = 1 - row["exposure_rate"] if row["exposure_rate"] is not None else None
    total = {k: sum(by_arm[a][k] for a in by_arm) for k in ("assigned", "eligible", "exposed", "excluded", "no_exposure")}
    total["exposure_rate"] = total["exposed"] / total["assigned"] if total["assigned"] else None
    total["attrition_rate"] = 1 - total["exposure_rate"] if total["exposure_rate"] is not None else None
    lost = [
        {
            "user_id": u.user_id,
            "variant": u.initial_variant,
            "reason": "excluded" if u.user_id in unit_exclusions else "no_exposure_event",
            "rules": [i["rule"] for i in unit_exclusions.get(u.user_id, [])],
        }
        for u in window_units
        if u.user_id not in exposed_ids
    ]
    diff = None
    if by_arm["control"]["exposure_rate"] is not None and by_arm["treatment"]["exposure_rate"] is not None:
        diff = by_arm["treatment"]["exposure_rate"] - by_arm["control"]["exposure_rate"]
    return {"by_arm": by_arm, "total": total, "differential_exposure_rate": diff, "lost_units": lost[:200]}


def _checks(
    *,
    exp,
    version,
    window_units,
    multi_units,
    late_received,
    late_occurred,
    duplicate_events,
    orphan_events,
    stale_metric_events,
    balance,
    lag_values,
    unit_exclusions,
) -> list[dict]:
    cross_variant = [u for u in window_units if u.switched]
    cross_device = [u for u in window_units if len(u.devices) > 1]
    checks = []

    if exp.allow_switch:
        status = "pass" if not multi_units else "info"
    else:
        status = "fail" if cross_variant else "pass"
    checks.append(
        {
            "key": "repeated_exposure",
            "label": "Repeated exposure",
            "status": status,
            "value": len(multi_units),
            "detail": (
                f"{len(multi_units)} units were assigned more than once; {len(cross_variant)} appear in "
                f"different arms and {len(cross_device)} used more than one device. "
                + (
                    "Switching is allowed, so ITT keeps them in the initial arm while as-treated splits "
                    "their outcomes by the arm that emitted each event."
                    if exp.allow_switch
                    else "Switching is not allowed, so these units are excluded with reason variant_conflict."
                )
                if multi_units
                else "Every unit has exactly one assignment."
            ),
            "items": [
                {
                    "user_id": u.user_id,
                    "assignments": len(u.assignments),
                    "variants": u.variants,
                    "devices": sorted(u.devices),
                    "initial_variant": u.initial_variant,
                    "rules": [i["rule"] for i in unit_exclusions.get(u.user_id, [])],
                }
                for u in multi_units[:25]
            ],
        }
    )

    # A device-level conflict is a single device id observed in two arms; that
    # is corruption rather than a user switching devices.
    device_conflict = []
    for unit in cross_device:
        arms_by_device: dict[str, set[str]] = defaultdict(set)
        for a in unit.assignments:
            arms_by_device[a.get("device_id") or "<none>"].add(a["variant"])
        if any(len(arms) > 1 for arms in arms_by_device.values()):
            device_conflict.append(unit)
    checks.append(
        {
            "key": "cross_device_consistency",
            "label": "Cross-device identity",
            "status": "warn" if device_conflict else "pass",
            "value": len(cross_device),
            "detail": (
                f"{len(cross_device)} users were seen on more than one device"
                + (
                    f"; {len(device_conflict)} of them show different arms on different devices, which is "
                    "expected only if they switched. Events are attributed to the user, not the device."
                    if device_conflict
                    else ". Events are attributed to the user, not the device."
                )
                if exp.cross_device
                else f"{len(cross_device)} units used more than one device while cross-device stitching is disabled."
                if cross_device
                else "No unit was observed on more than one device."
            ),
        }
    )

    checks.append(
        {
            "key": "duplicate_events",
            "label": "Duplicate event payloads",
            "status": "warn" if duplicate_events else "pass",
            "value": duplicate_events,
            "detail": (
                f"{duplicate_events} events repeated an identical payload under a new event id and were counted once."
                if duplicate_events
                else "No duplicate event payloads detected."
            ),
        }
    )

    checks.append(
        {
            "key": "orphan_events",
            "label": "Events with unknown assignment",
            "status": "warn" if orphan_events else "pass",
            "value": orphan_events,
            "detail": (
                f"{orphan_events} events referenced an assignment that was never ingested and were dropped."
                if orphan_events
                else "Every event maps to a known assignment."
            ),
        }
    )

    late_total = len(late_received) + len(late_occurred)
    checks.append(
        {
            "key": "late_results",
            "label": "Late-arriving results",
            "status": "warn" if late_total else "pass",
            "value": late_total,
            "detail": (
                f"{len(late_received)} events arrived after this version closed and "
                f"{len(late_occurred)} occurred after it closed. Both are excluded from the frozen sample "
                "and surfaced here; run verification to see how they would move the estimate."
                if late_total
                else "No results arrived or occurred after the version closed."
            ),
            "lag": _summarise_lag(lag_values),
        }
    )

    checks.append(
        {
            "key": "metric_definition",
            "label": "Metric definition drift",
            "status": "warn" if stale_metric_events else "pass",
            "value": stale_metric_events,
            "detail": (
                f"{stale_metric_events} events carried an outdated metric_version and were excluded."
                if stale_metric_events
                else "Every event matches the current metric definition."
            ),
        }
    )

    checks.append(
        {
            "key": "sample_ratio",
            "label": "Sample ratio mismatch",
            "status": "fail" if balance["srm"]["alert"] else "pass",
            "value": balance["srm"]["p_value"],
            "detail": (
                f"Arm sizes differ from the expected split (p={balance['srm']['p_value']:.2e}). "
                "Investigate before trusting the estimate."
                if balance["srm"]["alert"]
                else f"Arm sizes match the expected split (p={balance['srm']['p_value']:.3f})."
            ),
        }
    )

    checks.append(
        {
            "key": "allocation_integrity",
            "label": "Deterministic re-allocation",
            "status": "fail" if balance["allocation_integrity"]["alert"] else "pass",
            "value": balance["allocation_integrity"]["mismatch_rate"],
            "detail": (
                f"{balance['allocation_integrity']['mismatches']} of "
                f"{balance['allocation_integrity']['checked']} initial assignments do not match the splitter. "
                "Assignment data may be corrupt."
                if balance["allocation_integrity"]["mismatches"]
                else "Every ingested initial assignment matches the deterministic splitter."
            ),
        }
    )
    return checks


def _lag_values(events: list[dict]) -> list[float]:
    values = []
    for e in events:
        occ = _parse_ts(e.get("occurred_at"))
        rec = _parse_ts(e.get("received_at"))
        if occ and rec:
            values.append((rec - occ).total_seconds())
    return values


def _summarise_lag(lag_values: list[float]) -> dict | None:
    if not lag_values:
        return None
    ordered = sorted(lag_values)
    n = len(ordered)

    def pct(p: float) -> float:
        return ordered[min(n - 1, max(0, int(round(p * (n - 1)))))]

    return {
        "count": n,
        "median_seconds": pct(0.5),
        "p95_seconds": pct(0.95),
        "max_seconds": ordered[-1],
        "negative": sum(1 for v in ordered if v < 0),
    }


def _funnel(
    *,
    in_window,
    window_units,
    eligible,
    exposed,
    per_protocol,
    exclusion_summary,
    outside_window_assignments,
    truncated_assignments,
    excluded_units,
) -> list[dict]:
    return [
        {
            "stage": "Assignments inside version window",
            "count": len(in_window),
            "note": (
                f"{outside_window_assignments} outside the window"
                + (f", {truncated_assignments} before the truncation point" if truncated_assignments else "")
            ),
        },
        {
            "stage": "Distinct units",
            "count": len(window_units),
            "note": f"{sum(1 for u in window_units if len(u.assignments) > 1)} units had repeated assignments",
        },
        {
            "stage": "Contaminated units excluded",
            "count": excluded_units,
            "note": ", ".join(f"{k}: {v}" for k, v in sorted(exclusion_summary.items())) or "none",
        },
        {
            "stage": "Eligible units (ITT)",
            "count": len(eligible),
            "note": "arm fixed by initial assignment",
        },
        {
            "stage": "Exposed units (as-treated)",
            "count": len(exposed),
            "note": f"{len(eligible) - len(exposed)} eligible units had no exposure event",
        },
        {
            "stage": "Per-protocol units",
            "count": len(per_protocol),
            "note": f"{len(eligible) - len(per_protocol)} excluded for crossover or missing exposure",
        },
    ]


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------
def build_snapshot(result: dict, version, exp, *, as_of: str | None) -> dict:
    metrics = []
    itt = result["populations"]["intention_to_treat"]
    ast = result["populations"]["as_treated"]
    for itt_metric, ast_metric in zip(itt["metrics"], ast["metrics"]):
        metrics.append(
            {
                "key": itt_metric["metric"],
                "type": itt_metric["type"],
                "metric_version": itt_metric["metric_version"],
                "itt_diff": itt_metric["effect"]["diff"],
                "as_treated_diff": ast_metric["effect"]["diff"],
                "itt_p_value": itt_metric["effect"]["p_value"],
                "as_treated_p_value": ast_metric["effect"]["p_value"],
            }
        )
    diag = result["diagnostics"]
    return {
        "version_number": version.number,
        "version_id": version.id,
        "kind": version.kind,
        "status": version.status,
        "window": {"start": result["window"]["start"], "end": result["window"]["end"]},
        "as_of": as_of,
        "config_hash": hashlib.sha256(json.dumps(version.config, sort_keys=True).encode()).hexdigest(),
        "metric_definitions": exp.metrics,
        "counts": {
            "assignments": diag["assignments_in_window"],
            "events": diag["events_in_window"],
            "units": diag["units"],
            "units_excluded": diag["units_excluded"],
            "units_by_population": {name: pop["units"] for name, pop in result["populations"].items()},
        },
        "exclusions_by_rule": result["exclusions"]["by_rule"],
        "included_assignment_ids": result["_included_assignment_ids"],
        "included_event_ids": result["_included_event_ids"],
        "assignment_ids_hash": _sha(result["_included_assignment_ids"]),
        "event_ids_hash": _sha(result["_included_event_ids"]),
        "metrics": metrics,
    }


# ---------------------------------------------------------------------------
# Late-result verification against a frozen snapshot
# ---------------------------------------------------------------------------
def verify_snapshot(frozen: dict, current: dict) -> dict:
    """Compare a frozen snapshot with a live recomputation.

    Surfaces late results (events now included that the snapshot never saw),
    deleted data and repeated exposure (units that gained assignments after the
    snapshot was taken), plus how far the estimate would move.
    """
    frozen_events = set(frozen.get("included_event_ids") or [])
    current_events = set(current["_included_event_ids"])
    frozen_assignments = set(frozen.get("included_assignment_ids") or [])
    current_assignments = set(current["_included_assignment_ids"])
    new_events = sorted(current_events - frozen_events)
    removed_events = sorted(frozen_events - current_events)
    new_assignments = sorted(current_assignments - frozen_assignments)
    removed_assignments = sorted(frozen_assignments - current_assignments)

    current_by_metric = {
        m["metric"]: m for m in current["populations"]["intention_to_treat"]["metrics"]
    }
    shift = []
    for m in frozen.get("metrics", []):
        cur = current_by_metric.get(m["key"])
        cur_diff = cur["effect"]["diff"] if cur else None
        shift.append(
            {
                "metric": m["key"],
                "frozen_itt_diff": m.get("itt_diff"),
                "current_itt_diff": cur_diff,
                "delta": (
                    cur_diff - m["itt_diff"]
                    if cur_diff is not None and m.get("itt_diff") is not None
                    else None
                ),
            }
        )
    return {
        "snapshot_is_frozen": bool(frozen.get("frozen_at")),
        "frozen_at": frozen.get("frozen_at"),
        "frozen_counts": frozen.get("counts"),
        "current_counts": current["snapshot"]["counts"],
        "new_events": new_events[:100],
        "new_event_count": len(new_events),
        "removed_events": removed_events[:100],
        "removed_event_count": len(removed_events),
        "new_assignments": new_assignments[:100],
        "new_assignment_count": len(new_assignments),
        "removed_assignments": removed_assignments[:100],
        "removed_assignment_count": len(removed_assignments),
        "event_ids_hash_match": frozen.get("event_ids_hash") == current["snapshot"]["event_ids_hash"],
        "assignment_ids_hash_match": frozen.get("assignment_ids_hash")
        == current["snapshot"]["assignment_ids_hash"],
        "late_results": current["late_results"],
        "metric_shift": shift,
        "verdict": (
            "clean"
            if not new_events
            and not removed_events
            and not new_assignments
            and not removed_assignments
            else "drift"
        ),
    }


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------
def compare_versions(left: dict, right: dict) -> dict:
    def metric_map(result: dict, population: str) -> dict:
        return {m["metric"]: m for m in result["populations"][population]["metrics"]}

    left_itt = metric_map(left, "intention_to_treat")
    right_itt = metric_map(right, "intention_to_treat")
    left_ast = metric_map(left, "as_treated")
    right_ast = metric_map(right, "as_treated")
    rows = []
    for key in sorted(set(left_itt) | set(right_itt)):
        l, r = left_itt.get(key), right_itt.get(key)
        rows.append(
            {
                "metric": key,
                "name": (r or l)["name"],
                "type": (r or l)["type"],
                "metric_version_left": (l or {}).get("metric_version"),
                "metric_version_right": (r or {}).get("metric_version"),
                "left_diff": l["effect"]["diff"] if l else None,
                "right_diff": r["effect"]["diff"] if r else None,
                "left_p": l["effect"]["p_value"] if l else None,
                "right_p": r["effect"]["p_value"] if r else None,
                "left_ci": [l["effect"]["ci_low"], l["effect"]["ci_high"]] if l else None,
                "right_ci": [r["effect"]["ci_low"], r["effect"]["ci_high"]] if r else None,
                "delta": (
                    r["effect"]["diff"] - l["effect"]["diff"]
                    if l and r and l["effect"]["diff"] is not None and r["effect"]["diff"] is not None
                    else None
                ),
                "as_treated_left": left_ast.get(key, {}).get("effect", {}).get("diff"),
                "as_treated_right": right_ast.get(key, {}).get("effect", {}).get("diff"),
            }
        )
    return {
        "left": _version_summary(left),
        "right": _version_summary(right),
        "metrics": rows,
        "sample_delta": {
            "units_left": left["diagnostics"]["units"],
            "units_right": right["diagnostics"]["units"],
            "excluded_left": left["diagnostics"]["units_excluded"],
            "excluded_right": right["diagnostics"]["units_excluded"],
            "exclusions_by_rule_left": left["exclusions"]["by_rule"],
            "exclusions_by_rule_right": right["exclusions"]["by_rule"],
            "units_by_population_left": {
                k: v["units"] for k, v in left["populations"].items()
            },
            "units_by_population_right": {
                k: v["units"] for k, v in right["populations"].items()
            },
        },
        "warnings": _comparison_warnings(left, right),
    }


def _version_summary(result: dict) -> dict:
    return {
        "version_number": result["version"]["number"],
        "label": result["version"]["label"],
        "kind": result["version"]["kind"],
        "status": result["version"]["status"],
        "window": result["window"],
        "counts": result["snapshot"]["counts"],
        "event_ids_hash": result["snapshot"]["event_ids_hash"],
        "assignment_ids_hash": result["snapshot"]["assignment_ids_hash"],
    }


def _comparison_warnings(left: dict, right: dict) -> list[str]:
    warnings = []
    l_start, l_end = left["window"]["start"] or "", left["window"]["end"] or "9999"
    r_start, r_end = right["window"]["start"] or "", right["window"]["end"] or "9999"
    if (
        left["snapshot"]["window"] == right["snapshot"]["window"]
        and left["snapshot"]["event_ids_hash"] == right["snapshot"]["event_ids_hash"]
    ):
        warnings.append(
            "Both versions replay an identical frozen window and sample; any difference comes from the rules, "
            "not from the data."
        )
    elif l_start and r_start and l_start < r_end and r_start < l_end:
        warnings.append("Version windows overlap; observations may be counted in both versions.")
    if left["version"]["changed_metrics"] or right["version"]["changed_metrics"]:
        warnings.append(
            "At least one metric changed definition between these versions; the delta is not a like-for-like comparison."
        )
    if left["diagnostics"]["units"] == 0 or right["diagnostics"]["units"] == 0:
        warnings.append("One of the versions has no analysed units.")
    if left["exclusions"]["by_rule"] != right["exclusions"]["by_rule"]:
        warnings.append("Exclusion composition differs; the two samples are not directly comparable.")
    if left["snapshot"]["assignment_ids_hash"] != right["snapshot"]["assignment_ids_hash"]:
        warnings.append("The frozen assignment sets differ between versions.")
    return warnings
