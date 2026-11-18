"""Experiment lifecycle services: create, version, pause, roll back, verify."""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

from . import engine
from .allocation import allocate
from .models import DEFAULT_CONFIG, METRIC_TYPES, deep_merge, validate_config_patch
from .store import Store, utcnow

VERSION_KINDS = ("config_change", "pause", "resume", "rollback", "metric_change")


# ---------------------------------------------------------------------------
# Experiment creation
# ---------------------------------------------------------------------------
def create_experiment(store: Store, payload: dict) -> dict:
    for key in ("id", "name", "metrics"):
        if not payload.get(key):
            raise ValueError(f"experiment requires {key}")
    if store.get_experiment(payload["id"]):
        raise ValueError(f"experiment {payload['id']!r} already exists")
    metrics = payload["metrics"]
    if not isinstance(metrics, list) or not metrics:
        raise ValueError("metrics must be a non-empty list")
    seen = set()
    for metric in metrics:
        for key in ("key", "name", "type"):
            if not metric.get(key):
                raise ValueError(f"metric requires {key}")
        if metric["type"] not in METRIC_TYPES:
            raise ValueError(f"metric type must be one of {METRIC_TYPES}")
        if metric["key"] in seen:
            raise ValueError(f"duplicate metric key {metric['key']!r}")
        seen.add(metric["key"])
        metric.setdefault("version", "1")
        if metric["type"] == "continuous":
            metric.setdefault("aggregation", "sum")
    allocation_method = payload.get("allocation_method", "fixed")
    if allocation_method not in ("fixed", "stratified"):
        raise ValueError("allocation_method must be fixed or stratified")
    if allocation_method == "stratified" and not payload.get("strata_columns"):
        raise ValueError("stratified allocation requires strata_columns")
    switch_policy = payload.get("switch_policy", "last_wins")
    if switch_policy not in ("last_wins", "first_wins", "as_assigned"):
        raise ValueError("switch_policy must be last_wins, first_wins or as_assigned")

    store.create_experiment(
        {
            **payload,
            "allocation_method": allocation_method,
            "strata_columns": payload.get("strata_columns", []),
            "cross_device": bool(payload.get("cross_device")),
            "allow_switch": payload.get("allow_switch", True),
            "switch_policy": switch_policy,
            "metrics": metrics,
        }
    )
    config = deep_merge(DEFAULT_CONFIG, payload.get("config", {}))
    validate_config_patch(payload.get("config", {}), metrics)
    config["metric_versions"] = {m["key"]: str(m.get("version", "1")) for m in metrics}
    config["metric_definitions"] = metrics
    version = store.create_version(
        payload["id"],
        label=payload.get("version_label", "Initial version"),
        kind="initial",
        status="active",
        config=config,
        snapshot={},
        reason=payload.get("reason", "Experiment created"),
        effective_from=payload.get("effective_from") or utcnow(),
        window_start=payload.get("effective_from") or utcnow(),
        window_end=payload.get("effective_to"),
        window_mode="own",
    )
    _recompute_and_freeze(store, payload["id"], version.id)
    return get_experiment_detail(store, payload["id"])


# ---------------------------------------------------------------------------
# Version changes
# ---------------------------------------------------------------------------
def open_version(store: Store, exp_id: str, payload: dict) -> dict:
    exp = _require_experiment(store, exp_id)
    latest = exp.latest
    if latest is None:
        raise ValueError("experiment has no initial version")
    kind = payload.get("kind", "config_change")
    if kind not in VERSION_KINDS:
        raise ValueError(f"kind must be one of {VERSION_KINDS}")
    patch = payload.get("config", {}) or {}
    validate_config_patch(patch, exp.metrics)

    effective_from = payload.get("effective_from") or utcnow()
    previous = latest

    if kind in ("pause", "rollback") and previous.status in ("active", "paused"):
        store.set_version_status(previous.id, "paused" if kind == "pause" else "rolled_back", effective_from)
        store.close_window(previous.id, effective_from)
        _recompute_and_freeze(store, exp_id, previous.id)
    elif kind == "resume":
        # The resume boundary is the pause's end; nothing to close beyond the
        # frozen pause version already recorded.
        pass

    if kind == "rollback":
        target_number = payload.get("rollback_to")
        if target_number is None:
            raise ValueError("rollback requires rollback_to (a version number)")
        target = next((v for v in exp.versions if v.number == int(target_number)), None)
        if target is None:
            raise ValueError(f"unknown rollback target version {target_number}")
        # Re-read after the close above so the replay uses the target's
        # *final* frozen window rather than the open one.
        target = store.get_version(exp_id, target.id)
        config = deep_merge(target.config, patch)
        label = payload.get("label") or f"Rollback to v{target.number}"
        reason = payload.get("reason") or f"Rollback to version {target.number}"
        window_mode = "rollback_base"
    else:
        target = previous
        config = deep_merge(latest.config, patch)
        label = payload.get("label") or {
            "pause": "Paused",
            "resume": "Resumed",
            "metric_change": "Metric definition change",
        }.get(kind, "Config change")
        reason = payload.get("reason", "")
        window_mode = "own"

    # The sample window is the *analysis population* the version is evaluated
    # on. A config / resume version inherits the running window from the
    # version it supersedes so the same assignments stay analysable; a
    # rollback replays the target's frozen window.
    if window_mode == "rollback_base":
        # Replay exactly the window the target was evaluated on, so the
        # rollback is reproducible and directly comparable.
        frozen_window = target.snapshot.get("window") or {}
        window_start = (
            payload.get("window_start")
            or frozen_window.get("start")
            or target.window_start
            or target.effective_from
        )
        window_end = payload.get("window_end", frozen_window.get("end", target.window_end))
    elif kind == "pause":
        # A pause freezes this version's sample at the transition instant.
        window_start = payload.get("window_start") or latest.window_start or latest.effective_from
        window_end = payload.get("window_end", effective_from)
    elif kind == "resume":
        # Resuming opens a fresh sample window; earlier data belongs to the
        # pre-pause version and stays frozen there.
        window_start = payload.get("window_start") or effective_from
        window_end = payload.get("window_end")
    else:
        # Config / metric changes replay the whole experiment under the new
        # rules and stay open, so data recorded after the change is included.
        window_start = payload.get("window_start") or latest.window_start or latest.effective_from
        window_end = payload.get("window_end")

    status = {"pause": "paused", "rollback": "rolled_back"}.get(kind, "active")

    # A metric-definition change bumps the version of every affected metric so
    # events recorded under the old definition can be identified and excluded.
    metrics = [dict(m) for m in exp.metrics]
    changed = list(config.get("rules", {}).get("changed_metrics", []))
    bump = set(payload.get("bump_metrics", [])) or (set(changed) if kind == "metric_change" else set())
    for metric in metrics:
        if metric["key"] in bump:
            metric["version"] = str(int(metric.get("version", "1")) + 1)
    if bump:
        store.update_experiment_metrics(exp_id, metrics)
    config["metric_versions"] = {m["key"]: str(m.get("version", "1")) for m in metrics}
    config["metric_definitions"] = metrics

    version = store.create_version(
        exp_id,
        label=label,
        kind=kind,
        status=status,
        config=config,
        snapshot={},
        reason=reason,
        effective_from=effective_from,
        window_start=window_start,
        window_end=window_end,
        window_mode=window_mode,
        previous=previous,
    )
    # Re-freeze the superseded version at the boundary the new version implies.
    prior = store.get_version(exp_id, previous.id)
    if prior and prior.effective_to:
        _recompute_and_freeze(store, exp_id, prior.id)
    _recompute_and_freeze(store, exp_id, version.id)
    return get_experiment_detail(store, exp_id)


def recompute(store: Store, exp_id: str, version_id: int | None = None) -> dict:
    exp = _require_experiment(store, exp_id)
    version = exp.latest if version_id is None else store.get_version(exp_id, version_id)
    if version is None:
        raise ValueError("version not found")
    _recompute_and_freeze(store, exp_id, version.id)
    return analyze_version(store, exp_id, version.id)


def _recompute_and_freeze(store: Store, exp_id: str, version_id: int) -> dict:
    version = store.get_version(exp_id, version_id)
    result = analyze_version(store, exp_id, version_id, live=False, record_exclusions=True)
    snapshot = result["snapshot"]
    snapshot["frozen_at"] = version.window_end or version.effective_to
    snapshot["window_mode"] = version.window_mode
    snapshot["exclusions_detail"] = result["exclusions"]["unit_level"]
    snapshot["event_exclusions_detail"] = result["exclusions"]["event_level"]
    store.update_version_snapshot(version_id, snapshot)
    return result


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
def analyze_version(
    store: Store,
    exp_id: str,
    version_id: int,
    *,
    live: bool = False,
    record_exclusions: bool = False,
) -> dict:
    """Analyse a version.

    ``live=False`` (default) reproduces the frozen view: only events received
    up to the version's close time are counted. ``live=True`` includes results
    that arrived late and is what snapshot verification compares against.
    """
    exp = _require_experiment(store, exp_id)
    version = store.get_version(exp_id, version_id)
    if version is None:
        raise ValueError(f"unknown version {version_id}")
    as_of = None if live else (version.window_end or version.effective_to)
    events = store.all_events(exp_id)
    result = engine.analyze(
        exp,
        version,
        store.all_assignments(exp_id),
        events,
        as_of=as_of,
    )
    if live:
        # In the live view late events *are* analysed. Report them explicitly
        # so the drift is attributable rather than silent.
        close = version.window_end or version.effective_to
        if close:
            included = set(result["_included_event_ids"])
            late = [
                e for e in events
                if e["event_id"] in included
                and e.get("received_at") and e["received_at"] > close
            ]
            result["late_results"]["events_received_after_close"] = len(late)
            result["late_results"]["examples"] = [
                {
                    "event_id": e["event_id"],
                    "assignment_id": e["assignment_id"],
                    "metric_key": e["metric_key"],
                    "occurred_at": e["occurred_at"],
                    "received_at": e["received_at"],
                    "reason": "received_after_close",
                }
                for e in late[:20]
            ]
            lags = sorted(
                (datetime.fromisoformat(e["received_at"].replace("Z", "+00:00"))
                 - datetime.fromisoformat(e["occurred_at"].replace("Z", "+00:00"))).total_seconds()
                for e in late
            )
            if lags:
                # nearest-rank percentiles so p95 stays a real observation
                def pct(p: float) -> float:
                    return lags[min(len(lags) - 1, max(0, math.ceil(p * len(lags)) - 1))]

                result["late_results"]["late_lag"] = {
                    "count": len(lags),
                    "median_seconds": pct(0.5),
                    "p95_seconds": pct(0.95),
                    "max_seconds": lags[-1],
                }
    result["active_exclusions"] = store.list_exclusions(exp_id)
    if record_exclusions:
        items = []
        for item in result["exclusions"]["unit_level"]:
            items.append(
                {
                    "assignment_id": None,
                    "user_id": item["user_id"],
                    "reason": item["reason"],
                    "detail": item["detail"],
                    "rule": item["rule"],
                }
            )
        for item in result["exclusions"]["event_level"]:
            items.append(
                {
                    "assignment_id": item["assignment_id"],
                    "user_id": item["user_id"],
                    "reason": item["reason"],
                    "detail": item["detail"],
                    "rule": item["rule"],
                }
            )
        store.replace_exclusions(exp_id, version.id, items)
    return result


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------
def get_experiment_detail(store: Store, exp_id: str) -> dict:
    exp = _require_experiment(store, exp_id)
    versions = []
    for v in exp.versions:
        versions.append(
            {
                "id": v.id,
                "number": v.number,
                "label": v.label,
                "kind": v.kind,
                "status": v.status,
                "reason": v.reason,
                "effective_from": v.effective_from,
                "effective_to": v.effective_to,
                "window_start": v.window_start,
                "window_end": v.window_end,
                "window_mode": v.window_mode,
                "created_at": v.created_at,
                "frozen_at": v.snapshot.get("frozen_at"),
                "counts": v.snapshot.get("counts"),
                "exclusions_by_rule": v.snapshot.get("exclusions_by_rule"),
                "snapshot": v.snapshot,
                "config": v.config,
            }
        )
    latest_result = analyze_version(store, exp_id, exp.latest.id) if exp.latest else None
    return {
        "experiment": {
            "id": exp.id,
            "name": exp.name,
            "description": exp.description,
            "allocation_method": exp.allocation_method,
            "strata_columns": exp.strata_columns,
            "cross_device": exp.cross_device,
            "allow_switch": exp.allow_switch,
            "switch_policy": exp.switch_policy,
            "metrics": exp.metrics,
            "status": exp.status,
            "created_at": exp.created_at,
        },
        "versions": versions,
        "latest": latest_result,
        "exclusions": store.list_exclusions(exp_id),
        "audit": store.audit_log(exp_id),
    }


def compare(store: Store, exp_id: str, left_id: int, right_id: int, *, live: bool = False) -> dict:
    _require_experiment(store, exp_id)
    left = analyze_version(store, exp_id, left_id, live=live)
    right = analyze_version(store, exp_id, right_id, live=live)
    return engine.compare_versions(left, right)


def verify(store: Store, exp_id: str, version_id: int) -> dict:
    _require_experiment(store, exp_id)
    version = store.get_version(exp_id, version_id)
    if version is None:
        raise ValueError("version not found")
    frozen = version.snapshot or {}
    if not frozen.get("included_event_ids") and not frozen.get("included_assignment_ids"):
        raise ValueError("version has no frozen sample snapshot to verify against")
    current = analyze_version(store, exp_id, version_id, live=True)
    report = engine.verify_snapshot(frozen, current)
    report["version"] = {
        "id": version.id,
        "number": version.number,
        "label": version.label,
        "status": version.status,
        "frozen_at": frozen.get("frozen_at"),
    }
    return report


# ---------------------------------------------------------------------------
# Simulation / demo data
# ---------------------------------------------------------------------------
def simulate(
    store: Store,
    exp_id: str,
    *,
    n_units: int = 800,
    seed: int = 7,
    inject_late: bool = True,
) -> dict:
    """Generate assignments and events, deliberately injecting every pathology
    the tool is meant to surface."""
    exp = _require_experiment(store, exp_id)
    rng = random.Random(seed)
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    config = exp.latest.config
    version = exp.latest
    devices = ["web", "ios", "android", "tablet"]
    assignment_rows: list[dict] = []
    event_rows: list[dict] = []
    units = []

    def ts(hours: float) -> str:
        return (start + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")

    for i in range(n_units):
        user_id = f"u{i:05d}"
        strata = {}
        for col in exp.strata_columns:
            if col == "country":
                strata[col] = rng.choice(["US", "GB", "DE", "JP"])
            elif col == "platform":
                strata[col] = rng.choice(["ios", "android", "web"])
            else:
                strata[col] = rng.choice(["a", "b"])
        variant = allocate(config, exp.allocation_method, user_id, strata, exp.strata_columns)
        assigned_at = ts(i * 0.05)
        device = devices[i % 2]
        assignment_rows.append(
            {
                "assignment_id": f"a{i:05d}-1",
                "user_id": user_id,
                "variant": variant,
                "assigned_at": assigned_at,
                "strata": strata,
                "device_id": device,
                "source": "simulation",
            }
        )
        unit = {"user_id": user_id, "variant": variant, "device": device, "switch_aid": None}
        if exp.cross_device and rng.random() < 0.18:
            other = devices[(i + 1) % len(devices)]
            if other != device:
                assignment_rows.append(
                    {
                        "assignment_id": f"a{i:05d}-2",
                        "user_id": user_id,
                        "variant": variant,
                        "assigned_at": ts(i * 0.05 + 1),
                        "strata": strata,
                        "device_id": other,
                        "source": "simulation",
                    }
                )
        if exp.allow_switch and rng.random() < 0.07:
            switched = "treatment" if variant == "control" else "control"
            assignment_rows.append(
                {
                    "assignment_id": f"a{i:05d}-3",
                    "user_id": user_id,
                    "variant": switched,
                    "assigned_at": ts(72 + i * 0.05),
                    "strata": strata,
                    "device_id": device,
                    "source": "simulation",
                }
            )
            unit["switch_aid"] = f"a{i:05d}-3"
        units.append(unit)

    for i, unit in enumerate(units):
        if rng.random() >= 0.9:
            continue
        base = 0.32 + (0.05 if unit["variant"] == "treatment" else 0.0)
        event_rows.append(
            {
                "event_id": f"e{i:05d}-exp",
                "assignment_id": f"a{i:05d}-1",
                "event_type": "exposure",
                "metric_key": "exposure",
                "value": 1.0,
                "occurred_at": ts(i * 0.05 + 0.5),
                "properties": {"metric_version": "1"},
            }
        )
        for j, metric in enumerate(exp.metrics):
            if metric["key"] == "exposure":
                continue
            aid = unit["switch_aid"] or f"a{i:05d}-1"
            if metric["type"] == "proportion":
                if rng.random() < base:
                    event_rows.append(
                        {
                            "event_id": f"e{i:05d}-{metric['key']}",
                            "assignment_id": aid,
                            "event_type": metric["key"],
                            "metric_key": metric["key"],
                            "value": 1.0,
                            "occurred_at": ts(i * 0.05 + 2 + j),
                            "properties": {"metric_version": metric["version"]},
                        }
                    )
            else:
                revenue = round(
                    max(0.0, rng.gauss(12.0 + (2.0 if unit["variant"] == "treatment" else 0.0), 4.0)), 2
                )
                event_rows.append(
                    {
                        "event_id": f"e{i:05d}-{metric['key']}",
                        "assignment_id": aid,
                        "event_type": metric["key"],
                        "metric_key": metric["key"],
                        "value": revenue,
                        "occurred_at": ts(i * 0.05 + 2 + j),
                        "properties": {"metric_version": metric["version"]},
                    }
                )

    duplicates = []
    for row in rng.sample(event_rows, k=max(1, len(event_rows) // 120)):
        clone = dict(row)
        clone["event_id"] = row["event_id"] + "-dup"
        duplicates.append(clone)
    event_rows.extend(duplicates)

    late = []
    close = version.window_end
    if inject_late and close and event_rows:
        close_dt = datetime.fromisoformat(close.replace("Z", "+00:00"))
        for row in rng.sample(event_rows, k=max(1, len(event_rows) // 40)):
            clone = dict(row)
            clone["event_id"] = row["event_id"] + "-late"
            clone["occurred_at"] = (close_dt + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
            late.append(clone)
    event_rows.extend(late)

    junk = []
    for row in rng.sample(event_rows, k=max(1, len(event_rows) // 200)):
        clone = dict(row)
        clone["event_id"] = row["event_id"] + "-pre"
        clone["occurred_at"] = "2026-09-15T00:00:00Z"
        junk.append(clone)
    event_rows.extend(junk)

    report = {
        "assignments": store.insert_assignments(exp_id, assignment_rows),
        "events": store.insert_events(exp_id, event_rows),
        "generated": {
            "units": n_units,
            "assignment_rows": len(assignment_rows),
            "event_rows": len(event_rows),
            "duplicates_injected": len(duplicates),
            "late_injected": len(late),
            "pre_assignment_injected": len(junk),
        },
    }
    recompute(store, exp_id)
    return report


def inject_late_results(store: Store, exp_id: str, *, count: int = 8, seed: int = 3) -> dict:
    """Backfill a few results for *inside* the experiment window that only
    reach the warehouse after it closed. This is the classic late-arriving
    result: the frozen snapshot cannot contain them, and verification must
    surface the drift."""
    exp = _require_experiment(store, exp_id)
    frozen_version = next(
        (v for v in exp.versions if v.status in ("paused", "rolled_back") and v.window_end),
        None,
    )
    if frozen_version is None:
        raise ValueError("pause or roll back the experiment first to create a frozen window")
    close = frozen_version.window_end
    close_dt = datetime.fromisoformat(close.replace("Z", "+00:00"))
    rng = random.Random(seed)
    events = []
    metric_key = next((m["key"] for m in exp.metrics if m["type"] == "proportion" and m["key"] != "exposure"), None)
    if metric_key is None:
        metric_key = next((m["key"] for m in exp.metrics if m["key"] != "exposure"), None)
    if metric_key is None:
        raise ValueError("experiment has no outcome metric")
    assignments = store.all_assignments(exp_id)
    in_window = [
        a for a in assignments
        if frozen_version.window_start <= a["assigned_at"] < close
    ]
    for i, a in enumerate(rng.sample(in_window, k=min(count, len(in_window)))):
        occurred = (close_dt - timedelta(hours=6 + i)).isoformat().replace("+00:00", "Z")
        received = (close_dt + timedelta(days=2, hours=i)).isoformat().replace("+00:00", "Z")
        events.append(
            {
                "event_id": f"late-{a['assignment_id']}-{i}",
                "assignment_id": a["assignment_id"],
                "event_type": metric_key,
                "metric_key": metric_key,
                "value": 1.0,
                "occurred_at": occurred,
                "received_at": received,
                "properties": {"late_backfill": True},
            }
        )
    result = store.insert_events(exp_id, events)
    recompute(store, exp_id)
    return {"inserted": result, "frozen_version": frozen_version.number, "close": close}


# ---------------------------------------------------------------------------
# Import / export
# ---------------------------------------------------------------------------
def export_experiment(store: Store, exp_id: str) -> dict:
    detail = get_experiment_detail(store, exp_id)
    return {
        "exported_at": utcnow(),
        "experiment": detail["experiment"],
        "versions": [
            {
                "id": v["id"],
                "number": v["number"],
                "label": v["label"],
                "kind": v["kind"],
                "status": v["status"],
                "reason": v["reason"],
                "effective_from": v["effective_from"],
                "effective_to": v["effective_to"],
                "window_start": v["window_start"],
                "window_end": v["window_end"],
                "window_mode": v["window_mode"],
                "config": v["config"],
                "snapshot": v["snapshot"],
            }
            for v in detail["versions"]
        ],
        "assignments": store.all_assignments(exp_id),
        "events": store.all_events(exp_id),
        "exclusions": store.list_exclusions(exp_id),
        "exported_version_ids": [
            {"id": v["id"], "number": v["number"]} for v in detail["versions"]
        ],
    }


def import_bundle(store: Store, bundle: dict) -> dict:
    """Restore an exported bundle *verbatim*.

    Replaying the lifecycle would re-derive each version's config from the
    experiment's final metric definitions (so a v1 snapshot would silently be
    re-frozen under a later metric version). Import therefore writes the stored
    versions, their frozen snapshots and the exclusion ledger exactly as
    exported, then checks that the recomputed snapshots still agree.
    """
    exp_payload = bundle["experiment"]
    exp_id = exp_payload["id"]
    if store.get_experiment(exp_id):
        raise ValueError(f"experiment {exp_id!r} already exists")
    versions = bundle.get("versions") or []
    if not versions:
        raise ValueError("bundle has no versions")
    metrics = [dict(m) for m in exp_payload.get("metrics") or []]
    if not metrics:
        raise ValueError("bundle experiment has no metrics")
    for metric in metrics:
        metric.setdefault("version", "1")
        if metric.get("type") == "continuous":
            metric.setdefault("aggregation", "sum")

    collisions = store.fact_id_collisions(
        [a["assignment_id"] for a in bundle.get("assignments") or []],
        [e["event_id"] for e in bundle.get("events") or []],
    )
    if collisions["assignment_ids"] or collisions["event_ids"]:
        raise ValueError(
            "bundle fact ids already exist in this store "
            f"({collisions['assignment_ids']} assignment(s), {collisions['event_ids']} event(s)); "
            "import bundles into an empty store or re-export with fresh ids"
        )

    store.create_experiment(
        {
            **exp_payload,
            "allocation_method": exp_payload.get("allocation_method", "fixed"),
            "strata_columns": exp_payload.get("strata_columns", []),
            "cross_device": bool(exp_payload.get("cross_device")),
            "allow_switch": exp_payload.get("allow_switch", True),
            "switch_policy": exp_payload.get("switch_policy", "last_wins"),
            "metrics": metrics,
        }
    )

    for v in versions:
        store.restore_version(
            exp_id,
            number=int(v.get("number")),
            label=v.get("label") or f"Version {v.get('number')}",
            kind=v.get("kind", "initial"),
            status=v.get("status", "active"),
            config=v.get("config") or {},
            snapshot=v.get("snapshot") or {},
            reason=v.get("reason", ""),
            effective_from=v.get("effective_from"),
            effective_to=v.get("effective_to"),
            window_start=v.get("window_start"),
            window_end=v.get("window_end"),
            window_mode=v.get("window_mode", "own"),
        )

    if bundle.get("assignments"):
        store.insert_assignments(exp_id, bundle["assignments"])
    if bundle.get("events"):
        store.insert_events(exp_id, bundle["events"])

    # The ledger is keyed by version id, which differs in the target store.
    by_number = {v.number: v.id for v in store.list_versions(exp_id)}
    exported_by_id = {
        v.get("id"): v.get("number") for v in (bundle.get("exported_version_ids") or [])
    }
    for item in bundle.get("exclusions") or []:
        number = exported_by_id.get(item.get("version_id"))
        store.record_exclusions(
            exp_id,
            by_number.get(number) if number is not None else None,
            [
                {
                    "assignment_id": item.get("assignment_id"),
                    "user_id": item.get("user_id"),
                    "reason": item["reason"],
                    "rule": item["rule"],
                    "detail": item.get("detail"),
                }
            ],
        )

    # The stored snapshots are authoritative; recomputing the open version
    # refreshes its live view without touching the frozen ones.
    detail = get_experiment_detail(store, exp_id)
    return {
        **detail,
        "import_check": _import_check(store, exp_id, versions),
    }


def _import_check(store: Store, exp_id: str, exported_versions: list[dict]) -> list[dict]:
    """Compare each restored snapshot with a fresh frozen recompute."""
    out = []
    restored = {v.number: v for v in store.list_versions(exp_id)}
    for exported in exported_versions:
        version = restored.get(exported.get("number"))
        if version is None:
            out.append({"number": exported.get("number"), "status": "missing"})
            continue
        snapshot = version.snapshot or {}
        if not snapshot.get("included_event_ids"):
            out.append({"number": version.number, "status": "no snapshot"})
            continue
        current = engine.analyze(
            store.get_experiment(exp_id),
            version,
            store.all_assignments(exp_id),
            store.all_events(exp_id),
            as_of=version.window_end or version.effective_to,
        )
        out.append(
            {
                "number": version.number,
                "status": (
                    "match"
                    if snapshot.get("event_ids_hash") == current["snapshot"]["event_ids_hash"]
                    and snapshot.get("assignment_ids_hash")
                    == current["snapshot"]["assignment_ids_hash"]
                    else "mismatch"
                ),
                "frozen_events": len(snapshot.get("included_event_ids") or []),
                "recomputed_events": len(current["_included_event_ids"]),
            }
        )
    return out


def _require_experiment(store: Store, exp_id: str):
    exp = store.get_experiment(exp_id)
    if exp is None:
        raise KeyError(f"experiment {exp_id!r} not found")
    return exp
