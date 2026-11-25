import json
from dataclasses import replace

import pytest

from senti import focus, scanner
from senti.context import service_context
from senti.store import FOCUS_WEIGHT, Store, priority_of

from test_web import finding, request, serving


def priority_of_key(config, key):
    store = Store(config.db_path)
    try:
        return store.db.execute("SELECT priority FROM findings WHERE key = ?", (key,)).fetchone()["priority"]
    finally:
        store.close()


def test_a_focus_is_saved_for_every_instance_of_the_service_and_no_other(config, tmp_path):
    staging = replace(config, name="staging", db_path=str(tmp_path / "staging.db"))
    portal = replace(config, name="portal", service="portal", db_path=str(tmp_path / "portal.db"))
    configs = [config, staging, portal]
    focus.save(configs, config.service, "  Consents and payments  ")
    assert focus.get([staging], config.service) == "Consents and payments"
    assert focus.all_of(configs) == {config.service: "Consents and payments", "portal": ""}


def test_a_focus_must_be_for_a_service_and_not_too_long(config):
    with pytest.raises(ValueError):
        focus.save([config], "no-such-service", "x")
    with pytest.raises(ValueError):
        focus.save([config], config.service, "x" * (focus.MAX_LENGTH + 1))


def test_a_finding_in_focus_comes_sooner_until_the_focus_changes(config):
    store = Store(config.db_path)
    store.upsert_finding({**finding("in"), "in_focus": True})
    store.upsert_finding({**finding("out"), "in_focus": False})
    store.upsert_finding(finding("in"))  # re-imported without saying: stays in focus
    store.commit()
    store.close()
    plain = priority_of(4, 0.8, 1, "rising")
    assert priority_of_key(config, "in") == round(plain * FOCUS_WEIGHT, 3)
    assert priority_of_key(config, "out") == plain
    focus.save([config], config.service, "Something else")  # judged against the old focus: forgotten
    assert priority_of_key(config, "in") == plain


def test_every_ai_run_is_told_the_focus(config):
    assert "focus" not in service_context(config)
    focus.save([config], config.service, "Slow account lists")
    assert "Slow account lists" in service_context(config) and "in_focus" in service_context(config)
    assert "Slow account lists" in scanner.command(config)[2]


def test_the_page_saves_and_shows_the_focus(config):
    port, stop = serving([config])
    try:
        status, data = request(port, "POST", "/api/focus", {"service": config.service, "focus": "Logins"},
                               {"Content-Type": "application/json"})
        assert status == 200 and json.loads(data)["focus"] == "Logins"
        status, data = request(port, "GET", "/api/focus")
        assert status == 200 and json.loads(data)["focus"] == {config.service: "Logins"}
        status, _ = request(port, "POST", "/api/focus", {"service": config.service, "focus": 3},
                            {"Content-Type": "application/json"})
        assert status == 400
    finally:
        stop()
