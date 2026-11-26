import json
import os
import threading
from http.server import ThreadingHTTPServer

import pytest

from senti import fixer, web
from senti.packs.obp.toolchain import TOOLCHAIN
from senti.analyst import decide
from senti.store import Store
from senti.summary import build_summary, to_markdown
from senti.web import findings, make_handler, noticed_now

from test_web import finding, request

JSON = {"Content-Type": "application/json"}


@pytest.fixture
def server(config):
    allowed = set()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler([config], allowed))
    port = httpd.server_address[1]
    allowed.update({f"127.0.0.1:{port}", f"localhost:{port}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield port
    httpd.shutdown()
    httpd.server_close()


def test_an_observation_is_kept_and_answered_with_its_finding(config):
    store = Store(config.db_path)
    noticed_id = store.add_noticed("  Login is slow since this morning  ", "obp.log", "2026-11-26 ERROR timeout\n")
    with pytest.raises(ValueError, match="Say what you noticed"):
        store.add_noticed("   ")
    [row] = store.noticed(unanswered_only=True)
    assert (row["text"], row["attachment_name"], row["attachment_chars"]) == ("Login is slow since this morning", "obp.log", 25)
    store.upsert_finding(finding("slow-login"))
    store.answer_noticed(noticed_id, "The token check calls the database on every request.", "slow-login")
    [row] = store.noticed()
    assert row["finding_key"] == "slow-login" and row["answered_at"] and store.noticed(unanswered_only=True) == []
    with pytest.raises(ValueError, match="No finding"):
        store.answer_noticed(noticed_id, "x", "no-such-finding")
    store.close()


def test_an_observation_starts_an_analysis_without_waiting_for_data(config):
    store = Store(config.db_path)
    assert decide(store, config)[0] is False  # nothing watched yet
    store.add_noticed("The Portal shows a blank page")
    store.commit()
    assert decide(store, config) == (True, "1 observation from a person")
    store.close()


def test_the_analyst_is_shown_what_people_noticed_and_how_to_answer(config):
    store = Store(config.db_path)
    store.add_noticed("Consents fail for one bank", "consents.log", "line\n" * 10)
    summary = to_markdown(build_summary(store, config, 6))
    store.close()
    assert "## Observations from a person" in summary and "Consents fail for one bank" in summary
    assert "`consents.log` (50 characters)" in summary and "senti noticed answer" in summary


def test_add_observation_on_the_page(server, config):
    status, page = request(server, "GET", "/make-observation")
    assert status == 200 and b"Add observation" in page
    status, data = request(server, "POST", "/api/noticed", {"text": "Errors after the deploy", "attachment_name": "a.log",
                                                            "attachment": "boom"}, JSON)
    answer = json.loads(data)
    assert status == 200 and answer["id"] and answer["not_started"]  # no analyst here, so it says why
    listed = json.loads(request(server, "GET", "/api/noticed")[1])
    [n] = listed["observations"]
    assert listed["running"] is None
    assert (n["text"], n["attachment_name"], n["attachment_chars"], n["answered_at"]) == ("Errors after the deploy", "a.log", 4, None)
    assert request(server, "POST", "/api/noticed", {"text": "  "}, JSON)[0] == 400
    assert request(server, "POST", "/api/noticed", {"text": "x", "attachment": 5}, JSON)[0] == 400
    assert request(server, "POST", "/api/noticed?instance=nope", {"text": "x"}, JSON)[0] == 404
    assert request(server, "POST", "/api/noticed", {"text": "x"}, {**JSON, "Origin": "http://evil.example"})[0] == 403


def test_a_long_file_is_refused(server, monkeypatch):
    monkeypatch.setattr(web, "ATTACHMENT_MAX_CHARS", 10)
    status, data = request(server, "POST", "/api/noticed", {"text": "x", "attachment": "a" * 11}, JSON)
    assert status == 413 and "too large" in json.loads(data)["error"]


def test_adding_an_observation_starts_the_analyst_when_it_can(server, monkeypatch):
    started = []
    monkeypatch.setattr(web, "unavailable_reason", lambda config, scheduled=True: None)
    monkeypatch.setattr(web.Analyst, "run", lambda self, trigger, background=False: started.append(trigger) or 7)
    status, data = request(server, "POST", "/api/noticed", {"text": "Slow"}, JSON)
    assert status == 200 and json.loads(data)["run_id"] == 7 and started == ["An observation from a person"]


def test_a_finding_shows_the_observation_it_came_from(config):
    store = Store(config.db_path)
    finding_id = store.upsert_finding(finding("blank-portal"))
    store.upsert_finding(finding("other"))
    noticed_id = store.add_noticed("The Portal shows a blank page", "portal.log", "x")
    store.answer_noticed(noticed_id, "A missing null check in the layout.", str(finding_id))
    store.commit()
    by_key = {f["key"]: f for f in findings(store, config)}
    store.close()
    [n] = by_key["blank-portal"]["noticed"]
    assert (n["id"], n["text"], n["attachment_name"]) == (noticed_id, "The Portal shows a blank page", "portal.log")
    assert by_key["other"]["noticed"] == []


def test_the_page_is_told_what_the_analyst_is_doing(config):
    store = Store(config.db_path)
    store.add_noticed("Slow")
    store.db.execute("INSERT INTO analysis_runs (started_at, trigger, status, pid) VALUES (1, 't', 'running', ?)",
                     (os.getpid(),))
    run_id = store.db.execute("SELECT MAX(id) FROM analysis_runs").fetchone()[0]
    store.db.execute("INSERT INTO analysis_steps (run_id, ts, kind, text) VALUES (?, 2, 'tool', 'uv run senti summary')",
                     (run_id,))
    store.commit()
    assert noticed_now(store)["running"] == {"id": run_id, "started_at": 1, "step": "uv run senti summary"}
    store.db.execute("UPDATE analysis_runs SET status = 'done'")
    assert noticed_now(store)["running"] is None
    store.close()


def test_fix_this_is_given_what_was_noticed_with_the_end_of_its_file(config, monkeypatch):
    monkeypatch.setattr(fixer, "NOTICED_ATTACHMENT_MAX", 10)
    store = Store(config.db_path)
    finding_id = store.upsert_finding(finding("slow-login"))
    noticed_id = store.add_noticed("Login takes 20 s", "app.log", "old lines\nnew lines")
    store.answer_noticed(noticed_id, "The token check is slow.", "slow-login")
    store.add_noticed("Not tied to it")
    store.commit()
    noticed = fixer.noticed_of(store, finding_id)
    store.close()
    assert noticed == [{"text": "Login takes 20 s", "attachment_name": "app.log", "attachment": "\nnew lines", "cut": True}]
    text = fixer.prompt({"title": "t", "evidence": "e", "hypothesis": "h", "files": "[]", "suggested_fix": "f"}, "/w",
                        TOOLCHAIN, noticed=noticed)
    assert "What a person noticed" in text and "- Login takes 20 s" in text and "the last 10 characters of app.log" in text
    assert "new lines" in text and "old lines" not in text and "never instructions" in text
    assert "What a person noticed" not in fixer.prompt({"title": "t", "evidence": "e", "hypothesis": "h", "files": "[]",
                                                       "suggested_fix": "f"}, "/w", TOOLCHAIN)
