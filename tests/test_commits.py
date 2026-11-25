import json
import subprocess
import threading
import time
from dataclasses import replace
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

from senti import commits
from senti.web import make_handler


def git(repo, *args, data=None):
    return subprocess.run(["git", "-C", str(repo), *args], input=data, capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path):
    """A checkout with one commit, then: a changed file, a deleted one, two new ones and a .env."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Test Person")
    git(repo, "config", "user.email", "test@example.com")
    (repo / "app.py").write_text("print('one')\n")
    (repo / "old.py").write_text("gone\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "First")
    (repo / "app.py").write_text("print('two')\n")
    (repo / "old.py").unlink()
    (repo / "token.py").write_text("def token(): pass\n")
    (repo / "test_token.py").write_text("def test_token(): pass\n")
    (repo / ".env").write_text("SECRET=do-not-send\n")
    return repo


@pytest.fixture
def checkout(config, repo):
    c = replace(config, obp_api_source=str(repo))
    return commits.checkout_of([c], c.service)


def test_changes_lists_every_kind_of_change_and_marks_secrets(checkout):
    listed = {c["path"]: c for c in commits.changes(checkout.source)}
    assert set(listed) == {"app.py", "old.py", "token.py", "test_token.py", ".env"}
    assert listed["app.py"]["state"] == "changed" and listed["old.py"]["state"] == "deleted"
    assert listed["token.py"]["state"] == "new" and not listed["token.py"]["staged"]
    assert listed[".env"]["secret"] and not listed["token.py"]["secret"]


def test_what_is_a_secret():
    assert commits.is_secret(".env") and commits.is_secret("apps/portal/.env.local") and commits.is_secret("keys/server.pem")
    assert not commits.is_secret(".env.example") and not commits.is_secret("src/env.ts")


def test_stage_replaces_what_was_staged_and_refuses_secrets_and_unknown_paths(checkout, repo):
    git(repo, "add", "app.py")
    assert commits.stage(checkout, ["token.py", "test_token.py"])["staged"] == ["test_token.py", "token.py"]
    with pytest.raises(ValueError, match="secrets"):
        commits.stage(checkout, [".env"])
    with pytest.raises(ValueError, match="not changed"):
        commits.stage(checkout, ["../elsewhere.py"])
    assert commits.staged_paths(checkout.source) == {"test_token.py", "token.py"}  # a refusal leaves it as it was


def test_a_group_commits_only_when_exactly_its_files_are_staged_and_the_others_stay(checkout, repo):
    commits.save_groups(checkout.source, [
        {"id": "g1", "subject": "Add tokens", "body": "", "files": ["token.py", "test_token.py"]},
        {"id": "g2", "subject": "Say two", "body": "", "files": ["app.py", "old.py"]},
    ])
    with pytest.raises(ValueError, match="Nothing is staged"):
        commits.commit(checkout, "g2", "Say two", "")
    commits.stage(checkout, ["token.py"])
    with pytest.raises(ValueError, match="not staged: test_token.py"):
        commits.commit(checkout, "g1", "Add tokens", "")
    # Groups can be committed in any order: the second first
    commits.stage(checkout, ["app.py", "old.py"])
    body = "The app said one.\nIt now says two, as asked.\n\nOld is gone."
    done = commits.commit(checkout, "g2", "Say two", body)
    assert done["subject"] == "Say two"
    assert git(repo, "log", "-1", "--format=%B") == "Say two\n\nThe app said one. It now says two, as asked.\n\nOld is gone.\n\n"
    state = commits.state(checkout)
    assert [g["id"] for g in state["groups"]] == ["g1"] and state["groups"][0]["files"] == ["token.py", "test_token.py"]
    assert state["not_grouped"] == [".env"]


def test_the_state_leaves_out_files_committed_outside_senti(checkout, repo):
    commits.save_groups(checkout.source, [{"id": "g1", "subject": "Add tokens", "body": "", "files": ["token.py", "test_token.py"]}])
    git(repo, "add", "token.py")
    git(repo, "commit", "-q", "-m", "Token, from the IDE")
    state = commits.state(checkout)
    assert state["groups"][0]["files"] == ["test_token.py"] and not state["groups"][0]["staged"]
    assert state["head"]["subject"] == "Token, from the IDE"


def test_a_file_is_in_one_group_only():
    groups = commits.save_groups("/nowhere", [{"id": "a", "subject": "A", "files": ["x", "y"]},
                                              {"id": "a", "subject": "B", "files": ["y", "z"]}])
    assert [g["id"] for g in groups] == ["a", "ax"] and groups[1]["files"] == ["z"]


def test_claudes_groups_hold_only_changed_files_and_nothing_is_left_out(checkout):
    listed = commits.changes(checkout.source)
    answer = "Here they are:\n" + json.dumps({"groups": [
        {"subject": "Add tokens.", "body": "Tokens.", "files": ["token.py", "test_token.py", "../../etc/passwd"]},
        {"subject": "Say two", "body": "", "files": ["app.py", "token.py"]},
    ]})
    groups = commits.parse_groups(answer, listed)
    assert [g["files"] for g in groups] == [["token.py", "test_token.py"], ["app.py"], ["old.py", ".env"]]
    assert groups[2]["subject"] == ""
    with pytest.raises(ValueError):
        commits.parse_groups("I could not do it", listed)


def test_grouping_sends_the_changes_on_stdin_without_secrets_and_keeps_the_groups(checkout, monkeypatch):
    sent = {}

    def fake_claude(cmd, env, timeout, on_step, stopped, stdin_text=None, result_max=2000):
        sent["cmd"], sent["prompt"] = cmd, stdin_text
        return None, 0.12, json.dumps({"groups": [{"subject": "Add tokens", "body": "Why.", "files": ["token.py"]}]}), "model"

    monkeypatch.setattr(commits, "run_claude", fake_claude)
    assert commits.suggest(checkout) == {"started": True}
    for _ in range(100):
        if not commits.state(checkout)["suggesting"]:
            break
        time.sleep(0.05)
    state = commits.state(checkout)
    assert state["suggest_error"] is None and state["cost_usd"] == 0.12
    assert state["groups"][0]["subject"] == "Add tokens" and len(state["groups"]) == 2  # the rest in a last group
    assert "print('two')" in sent["prompt"] and "def token(): pass" in sent["prompt"]
    assert "do-not-send" not in sent["prompt"] and ".env (new, holds secrets)" in sent["prompt"]
    assert "-p" in sent["cmd"] and not any("print(" in part for part in sent["cmd"])  # the prompt is not an argument


@pytest.fixture
def server(config, repo):
    c = replace(config, obp_api_source=str(repo))
    allowed = set()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler([c], allowed))
    port = httpd.server_address[1]
    allowed.add(f"127.0.0.1:{port}")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield port, c.service
    httpd.shutdown()
    httpd.server_close()


def call(port, method, path, body=None, headers=None):
    conn = HTTPConnection("127.0.0.1", port)
    conn.request(method, path, body=json.dumps(body) if body is not None else None,
                 headers={"Content-Type": "application/json", **(headers or {})})
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data


def test_the_changes_page_stages_and_commits_a_group(server, repo):
    port, service = server
    status, page = call(port, "GET", "/changes")
    assert status == 200 and b"<title>Senti Changes</title>" in page
    status, data = call(port, "GET", "/api/commits")
    assert status == 200 and json.loads(data)[0]["changed"] == 5
    q = f"?service={service}"
    status, _ = call(port, "POST", "/api/commits/groups" + q, {"groups": [{"id": "g1", "subject": "Add tokens", "body": "", "files": ["token.py"]}]})
    assert status == 200
    status, data = call(port, "POST", "/api/commits/stage" + q, {"files": [".env"]})
    assert status == 409 and "secrets" in json.loads(data)["error"]
    assert call(port, "POST", "/api/commits/stage" + q, {"files": ["token.py"]})[0] == 200
    status, data = call(port, "POST", "/api/commits/commit" + q, {"id": "g1", "subject": "Add tokens", "body": "So there are tokens."})
    assert status == 200 and git(repo, "log", "-1", "--format=%s").strip() == "Add tokens"
    status, data = call(port, "GET", "/api/commits/state" + q)
    assert status == 200 and json.loads(data)["groups"] == []
    assert call(port, "GET", "/api/commits/state?service=nothing")[0] == 404


def test_the_commit_page_refuses_another_origin(server):
    port, service = server
    status, _ = call(port, "POST", f"/api/commits/stage?service={service}", {"files": ["token.py"]},
                     headers={"Origin": "http://evil.example"})
    assert status == 403


def test_a_files_changes_are_shown_but_not_a_secrets_or_an_unchanged_files(checkout, repo):
    changed = commits.file_diff(checkout.source, "app.py")
    assert changed["state"] == "changed" and "-print('one')" in changed["diff"] and "+print('two')" in changed["diff"]
    new = commits.file_diff(checkout.source, "token.py")
    assert new["state"] == "new" and new["diff"] == "@@ new file, 1 lines @@\n+def token(): pass"
    assert "-gone" in commits.file_diff(checkout.source, "old.py")["diff"]
    secret = commits.file_diff(checkout.source, ".env")
    assert secret["diff"] is None and "secrets" in secret["note"]
    (repo / "picture.bin").write_bytes(b"\0\1\2")
    assert commits.file_diff(checkout.source, "picture.bin")["note"] == "A binary file, not shown."
    with pytest.raises(ValueError, match="not changed"):
        commits.file_diff(checkout.source, "../../etc/passwd")


def test_the_commit_page_shows_a_files_changes(server):
    port, service = server
    status, data = call(port, "GET", f"/api/commits/diff?service={service}&path=app.py")
    assert status == 200 and "+print('two')" in json.loads(data)["diff"]
    status, data = call(port, "GET", f"/api/commits/diff?service={service}&path=.env")
    assert status == 200 and "do-not-send" not in data.decode()
    assert call(port, "GET", f"/api/commits/diff?service={service}&path=nothing.py")[0] == 404
    assert call(port, "GET", f"/api/commits/diff?service={service}")[0] == 404



def test_the_page_gets_the_editor_link_zed_unless_one_is_set(config, checkout, monkeypatch):
    assert config.editor_url == "zed://file{path}" and commits.state(checkout)["editor_url"] == "zed://file{path}"
    monkeypatch.setenv("SENTINEL_EDITOR_URL", "vscode://file{path}")
    assert type(config).from_env().editor_url == "vscode://file{path}"


def test_a_folder_git_will_not_work_in_says_why(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert commits.git_problem(str(plain)) == f"{plain} is not a git checkout"
    assert commits.git_problem(str(tmp_path / "missing")).endswith("is not a folder")

    class Refused:
        returncode, stdout = 128, ""
        stderr = "fatal: detected dubious ownership in repository at '/x'"

    monkeypatch.setattr(commits, "run_git", lambda *args, **kwargs: Refused())
    said = commits.git_problem(str(tmp_path))
    assert "another user" in said and f"git config --global --add safe.directory {tmp_path}" in said
