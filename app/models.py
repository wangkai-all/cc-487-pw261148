"""Domain model for the experiment analysis tool.

An *experiment* is a versioned configuration plus a stream of immutable facts.
Configuration changes (metric definitions, pause / rollback, allocation
changes, contamination rules) open a new **version**; every version carries a
**sample snapshot** describing exactly which analysis population it was
evaluated on and why rows were excluded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

# Analysis populations, from broadest to narrowest.
POPULATION_CHOICES = [
    "assigned",
    "intention_to_treat",
    "as_treated",
    "per_protocol",
]

METRIC_TYPES = ("proportion", "continuous")


@dataclass
class Version:
    id: int
    experiment_id: str
    number: int
    label: str
    kind: str  # initial | config_change | pause | rollback | resume | metric_change
    status: str  # active | paused | rolled_back
    config: dict
    effective_from: str | None
    effective_to: str | None
    window_start: str | None
    window_end: str | None
    snapshot: dict
    window_mode: str
    reason: str
    created_at: str


@dataclass
class Experiment:
    id: str
    name: str
    allocation_method: str  # fixed | stratified
    strata_columns: list[str]
    cross_device: bool
    allow_switch: bool
    switch_policy: str  # last_wins | first_wins | as_assigned
    description: str
    metrics: list[dict]
    created_at: str
    versions: list[Version] = field(default_factory=list)
    latest: Version | None = None

    @property
    def status(self) -> str:
        return self.latest.status if self.latest else "draft"

    @property
    def config(self) -> dict:
        return self.latest.config if self.latest else {}

    def metric(self, key: str) -> dict | None:
        for metric in self.metrics:
            if metric["key"] == key:
                return metric
        return None

    @property
    def metric_keys(self) -> list[str]:
        return [m["key"] for m in self.metrics]


# ---------------------------------------------------------------------------
# Config schema helpers
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "fixed": {"salt": "e1", "treatment_share": 0.5},
    "stratified": {"salt": "e1", "stratum_weights": {}},
    "rules": {
        # user_id -> variant override
        "force_assignments": {},
        # assignment_id / device_id that must never enter analysis
        "contaminated_external_ids": [],
        "contaminated_reasons": {},
        # drop assignments before this timestamp (intentional truncation)
        "truncate_assigned_before": None,
        # metrics whose definition changed at this version
        "changed_metrics": [],
    },
}

CONFIG_MERGE_KEYS = {"fixed", "stratified", "rules"}


def deep_merge(base: dict, patch: dict) -> dict:
    out = json.loads(json.dumps(base))
    for key, value in patch.items():
        if key not in out:
            out[key] = value
        elif isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def validate_config_patch(config: dict, metrics: list[dict]) -> None:
    if "fixed" in config:
        fixed = config["fixed"]
        if not isinstance(fixed, dict):
            raise ValueError("fixed must be an object")
        share = fixed.get("treatment_share", 0.5)
        if not (0 <= float(share) <= 1):
            raise ValueError("treatment_share must be between 0 and 1")
    if "stratified" in config:
        strat = config["stratified"]
        if not isinstance(strat, dict):
            raise ValueError("stratified must be an object")
        weights = strat.get("stratum_weights", {})
        if not isinstance(weights, dict):
            raise ValueError("stratum_weights must be an object")
        for k, v in weights.items():
            if not (0 <= float(v) <= 1):
                raise ValueError(f"stratum weight for {k!r} must be in [0, 1]")
    if "rules" in config:
        rules = config["rules"]
        if not isinstance(rules, dict):
            raise ValueError("rules must be an object")
        force = rules.get("force_assignments", {})
        if not isinstance(force, dict):
            raise ValueError("force_assignments must be an object")
        for uid, variant in force.items():
            if str(variant) not in ("control", "treatment"):
                raise ValueError(f"force assignment for {uid!r} must be control or treatment")
        ext = rules.get("contaminated_external_ids", [])
        if not isinstance(ext, list):
            raise ValueError("contaminated_external_ids must be a list")
        changed = rules.get("changed_metrics", [])
        if not isinstance(changed, list):
            raise ValueError("changed_metrics must be a list")
        known = {m["key"] for m in metrics}
        unknown = [k for k in changed if k not in known]
        if unknown:
            raise ValueError(f"changed_metrics contains unknown metrics: {unknown}")


def normalize_assignment(row: dict) -> dict:
    """Coerce an ingested assignment row into the canonical shape."""
    required = ("assignment_id", "user_id", "variant", "assigned_at")
    for key in required:
        if not row.get(key):
            raise ValueError(f"assignment row missing {key}")
    if row["variant"] not in ("control", "treatment"):
        raise ValueError(f"variant must be control or treatment, got {row['variant']!r}")
    strata = row.get("strata") or {}
    if not isinstance(strata, dict):
        raise ValueError("strata must be an object")
    out = {
        "assignment_id": str(row["assignment_id"]),
        "user_id": str(row["user_id"]),
        "variant": row["variant"],
        "assigned_at": str(row["assigned_at"]),
        "strata": {str(k): str(v) for k, v in strata.items()},
        "device_id": str(row["device_id"]) if row.get("device_id") else None,
        "source": row.get("source") or "ingest",
    }
    return out


REQUIRED_EVENT_FIELDS = ("event_id", "assignment_id", "event_type", "metric_key", "value", "occurred_at")


def normalize_event(row: dict, metrics: Iterable[dict]) -> dict:
    for key in REQUIRED_EVENT_FIELDS:
        if row.get(key) in (None, ""):
            raise ValueError(f"event row missing {key}")
    metric = next((m for m in metrics if m["key"] == str(row["metric_key"])), None)
    if metric is None:
        raise ValueError(f"unknown metric {row['metric_key']!r}")
    value = float(row["value"])
    if metric["type"] == "proportion" and value not in (0.0, 1.0):
        raise ValueError(f"proportion metric {metric['key']!r} accepts only 0 or 1")
    out = {
        "event_id": str(row["event_id"]),
        "assignment_id": str(row["assignment_id"]),
        "event_type": str(row["event_type"]),
        "metric_key": metric["key"],
        "metric_type": metric["type"],
        "value": value,
        "occurred_at": str(row["occurred_at"]),
        "received_at": str(row["received_at"]) if row.get("received_at") else None,
        "properties": row.get("properties") or {},
    }
    if not isinstance(out["properties"], dict):
        raise ValueError("properties must be an object")
    return out
