"""Minimal dependency-free HTTP API + static file server."""

from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import service
from .store import Store, get_store

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _json_body(handler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if length == 0:
        return {}
    if length > 20 * 1024 * 1024:
        raise ApiError(413, "request body too large")
    raw = handler.rfile.read(length)
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError(400, f"invalid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise ApiError(400, "body must be a JSON object")
    return body


def make_handler(store: Store):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ExperimentAnalysis/1.0"

        def log_message(self, fmt, *args):  # quieter default logging
            if os.environ.get("EXP_HTTP_LOG"):
                super().log_message(fmt, *args)

        # -- helpers ---------------------------------------------------
        def _send(self, status: int, payload, content_type="application/json"):
            if isinstance(payload, (dict, list)):
                body = json.dumps(payload, default=str).encode()
            elif isinstance(payload, bytes):
                body = payload
            else:
                body = str(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: int, message: str):
            self._send(status, {"error": message})

        def _static(self, path: str):
            rel = path.lstrip("/") or "index.html"
            rel = posixpath.normpath(rel)
            if rel.startswith(".."):
                return self._error(404, "not found")
            full = os.path.join(STATIC_DIR, rel)
            if not os.path.isfile(full):
                return self._error(404, "not found")
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            with open(full, "rb") as fh:
                self._send(200, fh.read(), ctype)

        # -- routing ---------------------------------------------------
        def do_GET(self):
            try:
                self._route_get()
            except ApiError as exc:
                self._error(exc.status, str(exc))
            except KeyError as exc:
                self._error(404, str(exc))
            except Exception as exc:  # pragma: no cover - defensive
                self._error(500, f"{type(exc).__name__}: {exc}")

        def do_POST(self):
            try:
                self._route_post()
            except ApiError as exc:
                self._error(exc.status, str(exc))
            except KeyError as exc:
                self._error(404, str(exc))
            except ValueError as exc:
                self._error(400, str(exc))
            except Exception as exc:  # pragma: no cover - defensive
                self._error(500, f"{type(exc).__name__}: {exc}")

        def _route_get(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)
            parts = [p for p in path.split("/") if p]

            if not path.startswith("/api/"):
                return self._static(path)
            parts = parts[1:]  # drop "api"

            if parts == ["health"]:
                return self._send(200, {"ok": True, "experiments": len(store.list_experiments())})
            if parts == ["experiments"]:
                return self._send(200, {"experiments": [_summary(e) for e in store.list_experiments()]})
            if len(parts) >= 2 and parts[0] == "experiments":
                exp_id = urllib.parse.unquote(parts[1])
                if len(parts) == 2:
                    return self._send(200, service.get_experiment_detail(store, exp_id))
                action = parts[2]
                if action == "analysis":
                    version_id = int(query.get("version", ["0"])[0]) or None
                    exp = store.get_experiment(exp_id)
                    if exp is None:
                        raise ApiError(404, "experiment not found")
                    version_id = version_id or exp.latest.id
                    live = query.get("live", ["0"])[0] in ("1", "true")
                    return self._send(
                        200, service.analyze_version(store, exp_id, version_id, live=live)
                    )
                if action == "compare":
                    left = _required_int(query, "left")
                    right = _required_int(query, "right")
                    live = query.get("live", ["0"])[0] in ("1", "true")
                    return self._send(200, service.compare(store, exp_id, left, right, live=live))
                if action == "verify":
                    version_id = _required_int(query, "version")
                    return self._send(200, service.verify(store, exp_id, version_id))
                if action == "snapshots":
                    return self._send(200, {"snapshots": store.snapshot_history(exp_id)})
                if action == "exclusions":
                    return self._send(200, {"exclusions": store.list_exclusions(exp_id)})
                if action == "audit":
                    return self._send(200, {"audit": store.audit_log(exp_id)})
                if action == "export":
                    return self._send(200, service.export_experiment(store, exp_id))
            raise ApiError(404, f"no route for {path}")

        def _route_post(self):
            parsed = urllib.parse.urlparse(self.path)
            parts = [p for p in parsed.path.split("/") if p]
            if not parts or parts[0] != "api":
                raise ApiError(404, "not found")
            parts = parts[1:]
            if parts == ["experiments"]:
                created = service.create_experiment(store, _json_body(self))
                return self._send(201, created)
            if parts == ["demo"]:
                return self._send(201, _seed_demo(store, _json_body(self)))
            if parts == ["import"]:
                return self._send(201, service.import_bundle(store, _json_body(self)))
            if len(parts) >= 3 and parts[0] == "experiments":
                exp_id = urllib.parse.unquote(parts[1])
                action = parts[2]
                body = _json_body(self)
                if action == "versions":
                    return self._send(201, service.open_version(store, exp_id, body))
                if action == "recompute":
                    version_id = body.get("version_id")
                    return self._send(
                        200, service.recompute(store, exp_id, int(version_id) if version_id else None)
                    )
                if action == "simulate":
                    return self._send(
                        200,
                        service.simulate(
                            store,
                            exp_id,
                            n_units=int(body.get("n_units", 800)),
                            seed=int(body.get("seed", 7)),
                        ),
                    )
                if action == "assignments":
                    rows = body.get("assignments")
                    if not isinstance(rows, list):
                        raise ApiError(400, "assignments must be a list")
                    return self._send(200, store.insert_assignments(exp_id, rows))
                if action == "events":
                    rows = body.get("events")
                    if not isinstance(rows, list):
                        raise ApiError(400, "events must be a list")
                    return self._send(200, store.insert_events(exp_id, rows))
                if action == "late-results":
                    return self._send(
                        200,
                        service.inject_late_results(
                            store, exp_id, count=int(body.get("count", 8))
                        ),
                    )
            raise ApiError(404, f"no route for {parsed.path}")

    return Handler


def _seed_demo(store: Store, body: dict) -> dict:
    """One-click demo: a stratified, cross-device experiment with three
    versions (initial → pause → metric change) and injected pathologies."""
    exp_id = body.get("id") or "checkout_v2"
    if store.get_experiment(exp_id):
        raise ApiError(409, f"experiment {exp_id!r} already exists")
    service.create_experiment(
        store,
        {
            "id": exp_id,
            "name": body.get("name") or "Checkout redesign",
            "description": "Stratified rollout of the one-page checkout, measured across web and app.",
            "allocation_method": "stratified",
            "strata_columns": ["country", "platform"],
            "cross_device": True,
            "allow_switch": True,
            "metrics": [
                {"key": "exposure", "name": "Exposure", "type": "proportion"},
                {"key": "purchase", "name": "Purchase", "type": "proportion"},
                {"key": "revenue", "name": "Revenue", "type": "continuous", "aggregation": "sum"},
            ],
            "effective_from": "2026-10-01T00:00:00Z",
            "config": {"stratified": {"stratum_weights": {"__default__": 0.5}}},
        },
    )
    service.simulate(store, exp_id, n_units=int(body.get("n_units", 1200)), seed=int(body.get("seed", 13)))
    service.open_version(
        store,
        exp_id,
        {"kind": "pause", "effective_from": "2026-10-08T00:00:00Z", "reason": "Guardrail: refund rate alert"},
    )
    service.open_version(
        store,
        exp_id,
        {
            "kind": "metric_change",
            "effective_from": "2026-10-09T00:00:00Z",
            "reason": "Revenue now nets out refunds",
            "config": {"rules": {"changed_metrics": ["revenue"]}},
        },
    )
    return service.get_experiment_detail(store, exp_id)


def _required_int(query: dict, key: str) -> int:
    if key not in query:
        raise ApiError(400, f"missing query parameter {key!r}")
    try:
        return int(query[key][0])
    except ValueError as exc:
        raise ApiError(400, f"{key} must be an integer") from exc


def _summary(exp) -> dict:
    latest = exp.latest
    return {
        "id": exp.id,
        "name": exp.name,
        "status": exp.status,
        "allocation_method": exp.allocation_method,
        "cross_device": exp.cross_device,
        "allow_switch": exp.allow_switch,
        "metric_count": len(exp.metrics),
        "versions": len(exp.versions),
        "latest_version": latest.number if latest else None,
        "created_at": exp.created_at,
        "counts": latest.snapshot.get("counts") if latest else None,
    }


def serve(host: str = "127.0.0.1", port: int = 8787, db: str | None = None) -> None:
    store = Store(db) if db else get_store()
    httpd = ThreadingHTTPServer((host, port), make_handler(store))
    print(f"Experiment analysis tool on http://{host}:{port}  (db: {store.path})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
