import logging
import tomllib
from dataclasses import replace

import pytest

from senti import logs
from senti.collector import Collector
from senti.config import Config
from senti.kinds import kind_of
from senti.model import SetupError, parse, to_toml
from senti.signatures import find_exception, parse_line, signature_of
from senti.sources import source_for
from senti.sources.senti import SentiSource
from senti.store import Store

SETUP = """
[[service]]
name = "senti"
kind = "python"
source = "/srv/work/Senti"
sources = ["senti"]

[[environment]]
name = "local"

[[instance]]
id = "senti-local"
service = "senti"
environment = "local"
"""

PYTHON = kind_of("python")


def test_a_senti_line_is_read_with_its_time_instance_logger_and_exception():
    raw = ("2026-11-25 03:01:42,123 ERROR [http://localhost:8080] senti.collector: Polling log:error failed\n"
           "Traceback (most recent call last):\n  File \"collector.py\", line 82, in poll_logs\n"
           "httpx.ConnectError: [Errno 111] Connection refused")
    parsed = parse_line(raw, PYTHON)
    assert parsed.ts and parsed.ts.year == 2026 and parsed.ts.microsecond == 123000
    assert (parsed.thread, parsed.logger, parsed.message) == ("http://localhost:8080", "senti.collector", "Polling log:error failed")
    assert find_exception(parsed, PYTHON) == "httpx.ConnectError"
    plain = parse_line("2026-11-25 03:01:42,123 WARNING senti.web: Could not read SonarCloud", PYTHON)
    assert (plain.thread, plain.logger, plain.message) == (None, "senti.web", "Could not read SonarCloud")


def test_two_lines_of_one_problem_have_one_signature():
    one = parse_line("2026-11-25 03:01:42,123 WARNING [http://a] senti.collector: Polling log:error failed: timed out after 30 s", PYTHON)
    two = parse_line("2026-11-25 04:11:02,456 WARNING [http://a] senti.collector: Polling log:error failed: timed out after 31 s", PYTHON)
    assert signature_of("warning", one, PYTHON).id == signature_of("warning", two, PYTHON).id


def test_senti_keeps_its_own_warnings_and_errors_newest_first():
    recent = logs.RecentLines()
    logger = logging.getLogger("senti.test-recent")
    logger.addHandler(recent)
    logger.propagate = False
    try:
        logger.setLevel(logging.INFO)
        logger.info("not kept")
        logger.warning("first warning")
        logger.warning("second warning")
        try:
            raise ValueError("bad value")
        except ValueError:
            logger.exception("it broke")
    finally:
        logger.removeHandler(recent)
    warnings = recent.newest_first("warning", 10)
    assert [w.split(": ", 1)[1] for w in warnings] == ["second warning", "first warning"]
    error = recent.newest_first("error", 10)[0]
    assert "ERROR senti.test-recent: it broke" in error and error.rstrip().endswith("ValueError: bad value")
    assert recent.newest_first("info", 10) == []


def test_senti_is_set_up_in_senti_toml_without_credentials_or_app_directory(tmp_path, monkeypatch, app_directories):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "senti.toml").write_text(SETUP)
    [senti] = Config.all_from_env()  # no app directory is read: none is set up for it
    assert (senti.name, senti.service, senti.kind, senti.read_from) == ("senti-local", "senti", "python", ("senti",))
    assert senti.obp_api_source == "/srv/work/Senti" and not senti.oidc_client_id and not senti.is_app
    assert isinstance(source_for(senti), SentiSource)
    assert senti.obp_base_url == f"http://{senti.ui_host}:{senti.ui_port}"  # Senti's own page: no url to keep in step
    monkeypatch.setenv("SENTINEL_UI_PORT", "9000")
    assert Config.from_env("senti-local").obp_base_url == f"http://{senti.ui_host}:9000"


def test_only_senti_may_leave_out_its_url():
    setup = parse(tomllib.loads(SETUP))
    assert setup.instance("senti-local").url == "" and "url" not in to_toml(setup).split("[[instance]]")[1]
    other = SETUP.replace('sources = ["senti"]', 'sources = ["loki"]').replace('[[instance]]', '[loki]\nurl = "http://loki"\n\n[[instance]]') + 'loki = \'{app="senti"}\'\n'
    with pytest.raises(SetupError, match="url is missing"):
        parse(tomllib.loads(other))


def test_the_collector_counts_sentis_own_errors(config, tmp_path, monkeypatch):
    recent = logs.RecentLines()
    monkeypatch.setattr(logs, "recent", recent)
    record = logging.LogRecord("senti.collector", logging.ERROR, __file__, 1, "Polling %s failed", ("log:error",), None)
    recent.handle(record)
    recent.handle(record)
    cfg = replace(config, name="senti-local", service="senti", kind="python", sources=("senti",), ignore_regex="",
                  log_levels=["error", "warning"])
    store = Store(str(tmp_path / "senti.db"))
    Collector(cfg, store, source_for(cfg)).poll_once()
    row = store.db.execute("SELECT logger, template, total_count FROM signatures").fetchone()
    assert tuple(row) == ("senti.collector", "Polling log:error failed", 2)
    assert source_for(cfg).running_commit()  # Senti's tests run from its checkout
