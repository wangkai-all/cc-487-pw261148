"""Deterministic split logic.

The splitter is the source of truth for *how a unit would be allocated*. It is
used both to validate ingested assignments (balance / integrity checks) and to
backfill units that were never ingested but did produce events.
"""

from __future__ import annotations

import hashlib


def _bucket(*parts: str) -> float:
    """Stable uniform value in [0, 1) derived from the provided parts."""
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return int(digest[:15], 16) / float(16**15)


def allocate_fixed(unit_id: str, salt: str, treatment_share: float) -> str:
    return "treatment" if _bucket(salt, "fixed", unit_id) < treatment_share else "control"


def allocate_stratified(
    unit_id: str, salt: str, stratum_key: str, stratum_weights: dict[str, float]
) -> str:
    share = float(stratum_weights.get(stratum_key, stratum_weights.get("__default__", 0.5)))
    return "treatment" if _bucket(salt, "strat", stratum_key, unit_id) < share else "control"


def allocate(config: dict, method: str, unit_id: str, strata: dict[str, str], strata_columns: list[str]) -> str:
    forced = config.get("rules", {}).get("force_assignments", {})
    if unit_id in forced:
        return forced[unit_id]
    if method == "stratified":
        strat = config.get("stratified", {})
        key = stratum_key(strata, strata_columns)
        return allocate_stratified(unit_id, strat.get("salt", "e1"), key, strat.get("stratum_weights", {}))
    fixed = config.get("fixed", {})
    return allocate_fixed(unit_id, fixed.get("salt", "e1"), float(fixed.get("treatment_share", 0.5)))


def stratum_key(strata: dict[str, str], strata_columns: list[str]) -> str:
    if not strata_columns:
        return "all"
    return "|".join(f"{col}={strata.get(col, '<missing>')}" for col in strata_columns)
