"""SQLite persistence layer (standard library only)."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from .models import Experiment, Version

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    allocation_method TEXT NOT NULL,
    strata_columns    TEXT NOT NULL DEFAULT '[]',
    cross_device      INTEGER NOT NULL DEFAULT 0,
    allow_switch      INTEGER NOT NULL DEFAULT 1,
    switch_policy     TEXT NOT NULL DEFAULT 'last_wins',
    metrics           TEXT NOT NULL DEFAULT '[]',
    status            TEXT NOT NULL DEFAULT 'draft',
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS versions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id  TEXT NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    number         INTEGER NOT NULL,
    label          TEXT NOT NULL,
    kind           TEXT NOT NULL,
    status         TEXT NOT NULL,
    config         TEXT NOT NULL,
    effective_from TEXT,
    effective_to   TEXT,
    window_start   TEXT,
    window_end     TEXT,
    snapshot       TEXT NOT NULL,
    window_mode    TEXT NOT NULL DEFAULT 'own',
    reason         TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    UNIQUE (experiment_id, number)
);

CREATE TABLE IF NOT EXISTS assignments (
    assignment_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    user_id       TEXT NOT NULL,
    variant       TEXT NOT NULL,
    assigned_at   TEXT NOT NULL,
    strata        TEXT NOT NULL DEFAULT '{}',
    device_id     TEXT,
    source        TEXT NOT NULL DEFAULT 'ingest'
);
CREATE INDEX IF NOT EXISTS idx_assignments_exp_user ON assignments(experiment_id, user_id);

CREATE TABLE IF NOT EXISTS events (
    event_id      TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    assignment_id TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    metric_key    TEXT NOT NULL,
    value         REAL NOT NULL,
    occurred_at   TEXT NOT NULL,
    properties    TEXT NOT NULL DEFAULT '{}',
    received_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_exp_assignment ON events(experiment_id, assignment_id);

CREATE TABLE IF NOT EXISTS exclusions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    version_id    INTEGER,
    assignment_id TEXT,
    user_id       TEXT,
    reason        TEXT NOT NULL,
    detail        TEXT NOT NULL DEFAULT '',
    rule          TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exclusions_exp ON exclusions(experiment_id);

CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    version_id    INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    created_at    TEXT NOT NULL,
    payload       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL,
    action        TEXT NOT NULL,
    detail        TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Store:
    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get("EXP_DB", os.path.join(os.getcwd(), "experiment_analysis.sqlite3"))
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # -- plumbing ---------------------------------------------------------
    @contextmanager
    def tx(self):
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def audit(self, experiment_id: str, action: str, detail: dict | None = None) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO audit_log (experiment_id, action, detail, created_at) VALUES (?,?,?,?)",
                (experiment_id, action, json.dumps(detail or {}, sort_keys=True), utcnow()),
            )

    # -- experiments ------------------------------------------------------
    def create_experiment(self, payload: dict) -> Experiment:
        exp_id = payload["id"]
        with self.tx() as conn:
            conn.execute(
                """INSERT INTO experiments
                   (id, name, description, allocation_method, strata_columns,
                    cross_device, allow_switch, switch_policy, metrics, status, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    exp_id,
                    payload["name"],
                    payload.get("description", ""),
                    payload.get("allocation_method", "fixed"),
                    json.dumps(payload.get("strata_columns", [])),
                    1 if payload.get("cross_device") else 0,
                    1 if payload.get("allow_switch", True) else 0,
                    payload.get("switch_policy", "last_wins"),
                    json.dumps(payload.get("metrics", [])),
                    "draft",
                    utcnow(),
                ),
            )
        self.audit(exp_id, "experiment.created", {"name": payload["name"]})
        return self.get_experiment(exp_id)

    def get_experiment(self, exp_id: str) -> Experiment | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM experiments WHERE id = ?", (exp_id,)).fetchone()
        if not row:
            return None
        exp = Experiment(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            allocation_method=row["allocation_method"],
            strata_columns=json.loads(row["strata_columns"]),
            cross_device=bool(row["cross_device"]),
            allow_switch=bool(row["allow_switch"]),
            switch_policy=row["switch_policy"],
            metrics=json.loads(row["metrics"]),
            created_at=row["created_at"],
        )
        exp.versions = self.list_versions(exp_id)
        exp.latest = exp.versions[-1] if exp.versions else None
        return exp

    def list_experiments(self) -> list[Experiment]:
        with self._lock:
            rows = self._conn.execute("SELECT id FROM experiments ORDER BY created_at").fetchall()
        return [self.get_experiment(r["id"]) for r in rows]

    def update_experiment_metrics(self, exp_id: str, metrics: list[dict]) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE experiments SET metrics = ? WHERE id = ?",
                (json.dumps(metrics), exp_id),
            )

    # -- versions ---------------------------------------------------------
    def _version_from_row(self, row: sqlite3.Row) -> Version:
        return Version(
            id=row["id"],
            experiment_id=row["experiment_id"],
            number=row["number"],
            label=row["label"],
            kind=row["kind"],
            status=row["status"],
            config=json.loads(row["config"]),
            effective_from=row["effective_from"],
            effective_to=row["effective_to"],
            window_start=row["window_start"],
            window_end=row["window_end"],
            snapshot=json.loads(row["snapshot"]),
            window_mode=row["window_mode"],
            reason=row["reason"],
            created_at=row["created_at"],
        )

    def list_versions(self, exp_id: str) -> list[Version]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM versions WHERE experiment_id = ? ORDER BY number", (exp_id,)
            ).fetchall()
        return [self._version_from_row(r) for r in rows]

    def get_version(self, exp_id: str, version_id: int) -> Version | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM versions WHERE experiment_id = ? AND id = ?", (exp_id, version_id)
            ).fetchone()
        return self._version_from_row(row) if row else None

    def latest_version(self, exp_id: str) -> Version | None:
        versions = self.list_versions(exp_id)
        return versions[-1] if versions else None

    def create_version(
        self,
        exp_id: str,
        *,
        label: str,
        kind: str,
        status: str,
        config: dict,
        snapshot: dict,
        reason: str = "",
        effective_from: str | None = None,
        effective_to: str | None = None,
        window_start: str | None = None,
        window_end: str | None = None,
        window_mode: str = "own",
        previous: Version | None = None,
    ) -> Version:
        number = (previous.number + 1) if previous else 1
        with self.tx() as conn:
            if previous is not None and previous.id is not None:
                # Supersede only the row that explicitly precedes this one;
                # a rollback reuses an earlier window and must not close it.
                conn.execute(
                    "UPDATE versions SET effective_to = COALESCE(effective_to, ?), "
                    "window_end = COALESCE(window_end, ?) WHERE id = ?",
                    (effective_from or utcnow(), window_end or effective_from or utcnow(), previous.id),
                )
            cur = conn.execute(
                """INSERT INTO versions
                   (experiment_id, number, label, kind, status, config,
                    effective_from, effective_to, window_start, window_end,
                    snapshot, window_mode, reason, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    exp_id,
                    number,
                    label,
                    kind,
                    status,
                    json.dumps(config, sort_keys=True),
                    effective_from,
                    effective_to,
                    window_start if window_start is not None else effective_from,
                    window_end,
                    json.dumps(snapshot, sort_keys=True),
                    window_mode,
                    reason,
                    utcnow(),
                ),
            )
            version_id = cur.lastrowid
            conn.execute(
                "UPDATE experiments SET status = ? WHERE id = ?", (status, exp_id)
            )
            conn.execute(
                "INSERT INTO snapshots (experiment_id, version_id, created_at, payload) VALUES (?,?,?,?)",
                (exp_id, version_id, utcnow(), json.dumps(snapshot, sort_keys=True)),
            )
        self.audit(exp_id, f"version.{kind}", {"number": number, "reason": reason})
        return self.get_version(exp_id, version_id)

    def restore_version(
        self,
        exp_id: str,
        *,
        number: int,
        label: str,
        kind: str,
        status: str,
        config: dict,
        snapshot: dict,
        reason: str = "",
        effective_from: str | None = None,
        effective_to: str | None = None,
        window_start: str | None = None,
        window_end: str | None = None,
        window_mode: str = "own",
    ) -> Version:
        """Insert a version verbatim (used by import).

        Unlike :meth:`create_version` this neither derives the number nor
        closes the preceding version's window — the exported bundle already
        records both, and replaying the lifecycle would re-derive configs from
        the *current* metric definitions instead of the ones each version was
        actually analysed under.
        """
        with self.tx() as conn:
            cur = conn.execute(
                """INSERT INTO versions
                   (experiment_id, number, label, kind, status, config,
                    effective_from, effective_to, window_start, window_end,
                    snapshot, window_mode, reason, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    exp_id,
                    number,
                    label,
                    kind,
                    status,
                    json.dumps(config, sort_keys=True),
                    effective_from,
                    effective_to,
                    window_start if window_start is not None else effective_from,
                    window_end,
                    json.dumps(snapshot, sort_keys=True),
                    window_mode,
                    reason,
                    utcnow(),
                ),
            )
            version_id = cur.lastrowid
            conn.execute(
                "UPDATE experiments SET status = ? WHERE id = ?", (status, exp_id)
            )
            conn.execute(
                "INSERT INTO snapshots (experiment_id, version_id, created_at, payload) VALUES (?,?,?,?)",
                (exp_id, version_id, utcnow(), json.dumps(snapshot, sort_keys=True)),
            )
        self.audit(exp_id, "version.restored", {"number": number, "kind": kind})
        return self.get_version(exp_id, version_id)

    def update_version_snapshot(self, version_id: int, snapshot: dict) -> None:
        with self.tx() as conn:
            row = conn.execute(
                "SELECT experiment_id FROM versions WHERE id = ?", (version_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown version {version_id}")
            conn.execute(
                "UPDATE versions SET snapshot = ? WHERE id = ?",
                (json.dumps(snapshot, sort_keys=True), version_id),
            )
            conn.execute(
                "UPDATE snapshots SET payload = ? WHERE version_id = ?",
                (json.dumps(snapshot, sort_keys=True), version_id),
            )

    def close_window(self, version_id: int, end: str) -> None:
        """Close the analysis window at ``end`` without touching status.

        Pausing or rolling back freezes the *sample* at the transition
        instant; the lifecycle status is set separately.
        """
        with self.tx() as conn:
            row = conn.execute(
                "SELECT experiment_id, window_start FROM versions WHERE id = ?", (version_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown version {version_id}")
            current = conn.execute(
                "SELECT window_end FROM versions WHERE id = ?", (version_id,)
            ).fetchone()
            if current["window_end"] is None:
                conn.execute(
                    "UPDATE versions SET window_end = ? WHERE id = ?", (end, version_id)
                )

    def set_version_status(self, version_id: int, status: str, effective_to: str | None) -> None:
        with self.tx() as conn:
            row = conn.execute(
                "SELECT experiment_id FROM versions WHERE id = ?", (version_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown version {version_id}")
            if effective_to is None:
                conn.execute("UPDATE versions SET status = ? WHERE id = ?", (status, version_id))
            else:
                conn.execute(
                    "UPDATE versions SET status = ?, effective_to = ? WHERE id = ?",
                    (status, effective_to, version_id),
                )
            conn.execute(
                "UPDATE experiments SET status = ? WHERE id = ?", (status, row["experiment_id"])
            )

    def snapshot_history(self, exp_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT s.*, v.number, v.label, v.kind
                   FROM snapshots s JOIN versions v ON v.id = s.version_id
                   WHERE s.experiment_id = ? ORDER BY v.number""",
                (exp_id,),
            ).fetchall()
        return [
            {
                "version_id": r["version_id"],
                "version_number": r["number"],
                "label": r["label"],
                "kind": r["kind"],
                "created_at": r["created_at"],
                "payload": json.loads(r["payload"]),
            }
            for r in rows
        ]

    # -- assignments / events --------------------------------------------
    def insert_assignments(self, exp_id: str, rows: list[dict]) -> dict:
        inserted, duplicates, errors = 0, 0, []
        with self.tx() as conn:
            for row in rows:
                try:
                    conn.execute(
                        """INSERT INTO assignments
                           (assignment_id, experiment_id, user_id, variant, assigned_at, strata, device_id, source)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (
                            row["assignment_id"],
                            exp_id,
                            row["user_id"],
                            row["variant"],
                            row["assigned_at"],
                            json.dumps(row.get("strata", {}), sort_keys=True),
                            row.get("device_id"),
                            row.get("source", "ingest"),
                        ),
                    )
                    inserted += 1
                except sqlite3.IntegrityError:
                    duplicates += 1
                except Exception as exc:  # pragma: no cover - defensive
                    errors.append({"assignment_id": row.get("assignment_id"), "error": str(exc)})
        self.audit(exp_id, "assignments.ingested", {"inserted": inserted, "duplicates": duplicates})
        return {"inserted": inserted, "duplicates": duplicates, "errors": errors}

    def insert_events(self, exp_id: str, rows: list[dict]) -> dict:
        inserted, duplicates, errors = 0, 0, []
        now = utcnow()
        with self.tx() as conn:
            for row in rows:
                try:
                    conn.execute(
                        """INSERT INTO events
                           (event_id, experiment_id, assignment_id, event_type, metric_key,
                            value, occurred_at, properties, received_at)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (
                            row["event_id"],
                            exp_id,
                            row["assignment_id"],
                            row["event_type"],
                            row["metric_key"],
                            row["value"],
                            row["occurred_at"],
                            json.dumps(row.get("properties", {}), sort_keys=True),
                            # Upstream feeds normally provide a receipt time. If
                            # they do not, assume the event was received when it
                            # occurred so frozen views stay reproducible.
                            row.get("received_at") or row["occurred_at"],
                        ),
                    )
                    inserted += 1
                except sqlite3.IntegrityError:
                    duplicates += 1
                except Exception as exc:  # pragma: no cover
                    errors.append({"event_id": row.get("event_id"), "error": str(exc)})
        self.audit(exp_id, "events.ingested", {"inserted": inserted, "duplicates": duplicates})
        return {"inserted": inserted, "duplicates": duplicates, "errors": errors}

    def fact_id_collisions(self, assignment_ids: list[str], event_ids: list[str]) -> dict:
        """Count fact ids already present in *any* experiment.

        Assignment and event ids are global primary keys, so importing a bundle
        whose ids already exist would silently drop rows (or, worse, attribute
        the other experiment's facts to this one).
        """
        def count(table: str, column: str, ids: list[str]) -> int:
            total = 0
            with self._lock:
                for i in range(0, len(ids), 500):
                    chunk = ids[i : i + 500]
                    marks = ",".join("?" for _ in chunk)
                    row = self._conn.execute(
                        f"SELECT COUNT(*) AS n FROM {table} WHERE {column} IN ({marks})",
                        chunk,
                    ).fetchone()
                    total += row["n"]
            return total

        return {
            "assignment_ids": count("assignments", "assignment_id", assignment_ids),
            "event_ids": count("events", "event_id", event_ids),
        }

    def all_assignments(self, exp_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM assignments WHERE experiment_id = ? ORDER BY user_id, assigned_at, assignment_id",
                (exp_id,),
            ).fetchall()
        return [
            {
                "assignment_id": r["assignment_id"],
                "user_id": r["user_id"],
                "variant": r["variant"],
                "assigned_at": r["assigned_at"],
                "strata": json.loads(r["strata"]),
                "device_id": r["device_id"],
                "source": r["source"],
            }
            for r in rows
        ]

    def all_events(self, exp_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE experiment_id = ? ORDER BY occurred_at, event_id",
                (exp_id,),
            ).fetchall()
        return [
            {
                "event_id": r["event_id"],
                "assignment_id": r["assignment_id"],
                "event_type": r["event_type"],
                "metric_key": r["metric_key"],
                "value": r["value"],
                "occurred_at": r["occurred_at"],
                "properties": json.loads(r["properties"]),
                "received_at": r["received_at"],
            }
            for r in rows
        ]

    # -- exclusions -------------------------------------------------------
    def record_exclusions(self, exp_id: str, version_id: int | None, items: list[dict]) -> None:
        if not items:
            return
        now = utcnow()
        with self.tx() as conn:
            conn.executemany(
                """INSERT INTO exclusions
                   (experiment_id, version_id, assignment_id, user_id, reason, detail, rule, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                [
                    (
                        exp_id,
                        version_id,
                        it.get("assignment_id"),
                        it.get("user_id"),
                        it["reason"],
                        json.dumps(it.get("detail"), sort_keys=True) if it.get("detail") is not None else "",
                        it["rule"],
                        now,
                    )
                    for it in items
                ],
            )

    def replace_exclusions(self, exp_id: str, version_id: int, items: list[dict]) -> None:
        with self.tx() as conn:
            conn.execute(
                "DELETE FROM exclusions WHERE experiment_id = ? AND version_id = ?",
                (exp_id, version_id),
            )
        self.record_exclusions(exp_id, version_id, items)

    def list_exclusions(self, exp_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM exclusions WHERE experiment_id = ? ORDER BY id", (exp_id,)
            ).fetchall()
        return [
            {
                "id": r["id"],
                "version_id": r["version_id"],
                "assignment_id": r["assignment_id"],
                "user_id": r["user_id"],
                "reason": r["reason"],
                "detail": json.loads(r["detail"]) if r["detail"] else None,
                "rule": r["rule"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def audit_log(self, exp_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM audit_log WHERE experiment_id = ? ORDER BY id", (exp_id,)
            ).fetchall()
        return [
            {
                "action": r["action"],
                "detail": json.loads(r["detail"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]


_default_store: Store | None = None


def get_store() -> Store:
    global _default_store
    if _default_store is None:
        _default_store = Store()
    return _default_store
