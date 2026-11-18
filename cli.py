#!/usr/bin/env python3
"""Command line interface for the experiment analysis tool."""

from __future__ import annotations

import argparse
import json
import sys

from app import service
from app.store import Store


def _store(args) -> Store:
    return Store(args.db)


def _print(payload) -> None:
    json.dump(payload, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def cmd_serve(args):
    from app.server import serve

    serve(host=args.host, port=args.port, db=args.db)


def cmd_create(args):
    store = _store(args)
    payload = json.loads(args.file) if args.file else {
        "id": args.id,
        "name": args.name,
        "allocation_method": args.allocation or "fixed",
        "strata_columns": args.strata_columns or [],
        "cross_device": args.cross_device,
        "allow_switch": args.allow_switch,
        "metrics": [
            {"key": "exposure", "name": "Exposure", "type": "proportion"},
            {"key": "purchase", "name": "Purchase", "type": "proportion"},
            {"key": "revenue", "name": "Revenue", "type": "continuous", "aggregation": "sum"},
        ],
        "effective_from": args.effective_from,
    }
    _print(service.create_experiment(store, payload))


def cmd_simulate(args):
    store = _store(args)
    _print(service.simulate(store, args.experiment, n_units=args.units, seed=args.seed))


def cmd_version(args):
    store = _store(args)
    payload = {"kind": args.kind, "reason": args.reason, "effective_from": args.effective_from}
    if args.config:
        payload["config"] = json.loads(args.config)
    if args.rollback_to:
        payload["rollback_to"] = args.rollback_to
    if args.window_end:
        payload["window_end"] = args.window_end
    _print(service.open_version(store, args.experiment, payload))


def cmd_analyze(args):
    store = _store(args)
    exp = service._require_experiment(store, args.experiment)
    version_id = args.version or exp.latest.id
    _print(service.analyze_version(store, args.experiment, version_id, live=args.live))


def cmd_verify(args):
    store = _store(args)
    _print(service.verify(store, args.experiment, args.version))


def cmd_compare(args):
    store = _store(args)
    _print(service.compare(store, args.experiment, args.left, args.right))


def cmd_ingest(args):
    store = _store(args)
    exp = service._require_experiment(store, args.experiment)
    with open(args.file, encoding="utf-8") as fh:
        rows = json.load(fh)
    if isinstance(rows, dict):
        rows = rows.get("assignments") or rows.get("events") or []
    if args.kind == "assignments":
        _print(store.insert_assignments(args.experiment, rows))
    else:
        _print(store.insert_events(args.experiment, rows))


def cmd_export(args):
    store = _store(args)
    _print(service.export_experiment(store, args.experiment))


def cmd_list(args):
    store = _store(args)
    _print(
        [
            {
                "id": e.id,
                "name": e.name,
                "status": e.status,
                "versions": len(e.versions),
                "allocation_method": e.allocation_method,
            }
            for e in store.list_experiments()
        ]
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None, help="SQLite file (default ./experiment_analysis.sqlite3)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the web UI + JSON API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("create", help="create an experiment")
    p.add_argument("--id")
    p.add_argument("--name")
    p.add_argument("--file", help="JSON payload (overrides the flags)")
    p.add_argument("--allocation", choices=["fixed", "stratified"])
    p.add_argument("--strata-columns", nargs="*")
    p.add_argument("--cross-device", action="store_true")
    p.add_argument("--allow-switch", action="store_true", default=True)
    p.add_argument("--effective-from")
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("simulate", help="generate demo assignments/events")
    p.add_argument("experiment")
    p.add_argument("--units", type=int, default=800)
    p.add_argument("--seed", type=int, default=7)
    p.set_defaults(func=cmd_simulate)

    p = sub.add_parser("version", help="open a new version (pause/rollback/config/metric change)")
    p.add_argument("experiment")
    p.add_argument("kind", choices=["config_change", "pause", "resume", "rollback", "metric_change"])
    p.add_argument("--reason", default="")
    p.add_argument("--config", help="JSON config patch")
    p.add_argument("--rollback-to", type=int, dest="rollback_to")
    p.add_argument("--effective-from")
    p.add_argument("--window-end")
    p.set_defaults(func=cmd_version)

    p = sub.add_parser("analyze", help="print a version analysis")
    p.add_argument("experiment")
    p.add_argument("--version", type=int)
    p.add_argument("--live", action="store_true", help="include late-arriving results")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("verify", help="verify a frozen snapshot against live data")
    p.add_argument("experiment")
    p.add_argument("version", type=int)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("compare", help="compare two versions")
    p.add_argument("experiment")
    p.add_argument("left", type=int)
    p.add_argument("right", type=int)
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("ingest", help="ingest assignments or events from JSON")
    p.add_argument("experiment")
    p.add_argument("kind", choices=["assignments", "events"])
    p.add_argument("file")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("export", help="export an experiment as JSON")
    p.add_argument("experiment")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("list", help="list experiments")
    p.set_defaults(func=cmd_list)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except (KeyError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
