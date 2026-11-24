"""The probes on Senti's web page, modelled on OBP-API's: `/alive` and `/health` say the process answers HTTP and check nothing else; `/status` checks what Senti needs to work (each instance's database and the source review's database), answers HTML or JSON depending on the Accept header, and answers 503 when a check fails.

A watched service being down is not Senti's failure (it is what Senti reports), so `/status` does not call the services it watches. It shows instance names and check results only: no paths, hosts or settings.
"""

import html
import json
import sqlite3
import time
from http import HTTPStatus
from importlib.metadata import PackageNotFoundError, version

from .config import Config

ALIVE_BODY = b"true"
HEALTH_BODY = b'{"status":"ok"}'
JSON_TYPE = "application/json; charset=utf-8"
HTML_TYPE = "text/html; charset=utf-8"
# How long a database check may wait for a lock before it counts as failed
DATABASE_TIMEOUT_SECONDS = 2.0

_started_monotonic = time.monotonic()


def _senti_version() -> str:
    try:
        return version("senti")
    except PackageNotFoundError:
        return "unknown"


def _database_check(db_path: str) -> str:
    """"ok" if the SQLite database opens read-only and answers a query, "fail" otherwise. Read-only, so a missing database is reported rather than created."""
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=DATABASE_TIMEOUT_SECONDS)
        try:
            connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
        finally:
            connection.close()
        return "ok"
    except sqlite3.Error:
        return "fail"


def run_checks(configs: list[Config]) -> dict:
    """Each instance's database, by instance name, and the source review's database (shared by all instances)."""
    databases = {config.name: _database_check(config.db_path) for config in configs}
    source_database = _database_check(configs[0].source_db_path) if configs else "fail"
    return {"databases": databases, "source_database": source_database}


def _all_ok(checks: dict) -> bool:
    return checks["source_database"] == "ok" and all(result == "ok" for result in checks["databases"].values())


def status_json(configs: list[Config]) -> dict:
    checks = run_checks(configs)
    return {
        "status": "ok" if _all_ok(checks) else "degraded",
        "version": _senti_version(),
        "uptime_seconds": int(time.monotonic() - _started_monotonic),
        "checks": checks,
    }


def render_status(data: dict) -> str:
    escape = html.escape
    rows = "".join(f"<tr><td>Database: {escape(instance_name)}</td><td><strong>{escape(result)}</strong></td></tr>"
                   for instance_name, result in data["checks"]["databases"].items())
    rows += f"<tr><td>Source review database</td><td><strong>{escape(data['checks']['source_database'])}</strong></td></tr>"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Senti - Status</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{{font-family:system-ui,sans-serif;max-width:52rem;margin:2rem auto;padding:0 1rem;line-height:1.5}}
td{{padding:.2rem 1rem .2rem 0}}</style></head><body>
<h1>Senti - Status: {escape(data["status"])}</h1>
<p>Version {escape(data["version"])} · up {data["uptime_seconds"]} seconds</p>
<table>{rows}</table>
</body></html>"""


def prefers_json(accept_header: str | None) -> bool:
    """True when the Accept header lists application/json, as OBP-API's /status decides."""
    media_ranges = [part.split(";", 1)[0].strip().lower() for part in (accept_header or "").split(",")]
    return "application/json" in media_ranges


def answer(path: str, accept_header: str | None, configs: list[Config]) -> tuple[int, bytes, str] | None:
    """(status code, body, content type) for a probe path, or None if the path is not a probe."""
    if path == "/alive":
        return HTTPStatus.OK, ALIVE_BODY, JSON_TYPE
    if path == "/health":
        return HTTPStatus.OK, HEALTH_BODY, JSON_TYPE
    if path == "/status":
        data = status_json(configs)
        code = HTTPStatus.OK if data["status"] == "ok" else HTTPStatus.SERVICE_UNAVAILABLE
        if prefers_json(accept_header):
            return code, json.dumps(data, indent=2).encode(), JSON_TYPE
        return code, render_status(data).encode(), HTML_TYPE
    return None
