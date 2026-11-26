import subprocess
from dataclasses import replace

from senti import build
from senti.store import Store

REPO = "ada/OBP-API"
API = "https://api.github.com/repos/ada/OBP-API/actions"


def run(id, name, conclusion="success", status="completed", sha="abcdef1234567", created="2026-10-19T10:00:00Z"):
    return {"id": id, "name": name, "status": status, "conclusion": conclusion, "head_sha": sha,
            "created_at": created, "html_url": f"https://github.com/ada/OBP-API/actions/runs/{id}"}


def fake(monkeypatch, runs, jobs=()):
    asked = []
    def get(url):
        asked.append(url)
        return {"jobs": list(jobs)} if "/jobs" in url else {"workflow_runs": runs}
    monkeypatch.setattr(build, "_get", get)
    return asked


def test_ok_when_the_latest_run_of_each_workflow_passed(monkeypatch):
    asked = fake(monkeypatch, [run(3, "Build", created="2026-10-19T11:00:00Z"), run(2, "Scan", "skipped"),
                               run(1, "Build", "failure")])  # an older failure of Build no longer counts
    shown = build.check(REPO, "develop")
    assert shown["state"] == "ok" and "in ada/OBP-API on develop passed, up to abcdef123" in shown["text"]
    assert asked == [f"{API}/runs?per_page=30&branch=develop"]


def test_failing_names_the_workflow_commit_job_and_step(monkeypatch):
    fake(monkeypatch, [run(5, "Build", "failure")],
         [{"name": "compile", "conclusion": "success", "steps": []},
          {"name": "test (3, v6 only)", "conclusion": "failure",
           "steps": [{"name": "Set up job", "conclusion": "success"}, {"name": "Run tests", "conclusion": "failure"}]}])
    shown = build.check(REPO, "develop")
    assert shown["state"] == "failing" and shown["url"].endswith("/runs/5")
    assert shown["text"] == "Build failure at abcdef123 in ada/OBP-API on develop: job test (3, v6 only) failed at step Run tests"


def test_running_and_unknown(monkeypatch):
    fake(monkeypatch, [run(7, "Build", None, "in_progress")])
    assert build.check(REPO, None)["state"] == "running"


def test_failure_gives_the_failed_jobs_steps_and_annotations(monkeypatch, config):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(build, "pushed_to", lambda source: (REPO, "develop"))
    def get(url, text=False):
        if "/annotations" in url:
            return [{"path": "obp-api/src/A.scala", "start_line": 12, "annotation_level": "failure",
                     "message": "not found: value x"}, {"annotation_level": "warning", "message": "deprecated"}]
        if "/jobs" in url:
            return {"jobs": [{"id": 9, "name": "compile", "conclusion": "failure",
                              "steps": [{"name": "Build", "conclusion": "failure"}]}]}
        return {"workflow_runs": [run(5, "Build", "failure")]}
    monkeypatch.setattr(build, "_get", get)
    failed = build.failure(config)
    assert failed["key"] == "build:ada/OBP-API:develop:Build:5"
    assert failed["run_id"] == 5 and failed["sha"] == "abcdef1234567"
    assert failed["details"] == "Job compile failed at step Build.\n- obp-api/src/A.scala:12 not found: value x"
    monkeypatch.setattr(build, "_get", lambda url, text=False: {"workflow_runs": [run(6, "Build")]})
    assert build.failure(config) is None


def git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def test_the_build_is_of_the_repository_and_branch_the_checkout_pushes_to(tmp_path):
    git(tmp_path, "init", "-q", "-b", "develop")
    assert build.pushed_to(str(tmp_path)) is None  # no remote
    git(tmp_path, "remote", "add", "origin", "git@github.com:ada/OBP-API.git")
    assert build.pushed_to(str(tmp_path)) == ("ada/OBP-API", "develop")
    git(tmp_path, "remote", "add", "fork", "https://github.com/bob/OBP-API")
    git(tmp_path, "config", "remote.pushDefault", "fork")
    git(tmp_path, "config", "push.default", "current")
    assert build.pushed_to(str(tmp_path)) == ("bob/OBP-API", "develop")
    git(tmp_path, "remote", "set-url", "fork", "https://gitlab.com/bob/OBP-API")
    assert build.pushed_to(str(tmp_path)) is None  # not GitHub
    assert build.pushed_to("") is None


def test_only_the_workflows_named_count(monkeypatch):
    fake(monkeypatch, [run(3, "Build"), run(2, "Publish", "failure")])
    assert build.check(REPO, "develop", ("Build",))["state"] == "ok"
    assert build.check(REPO, "develop")["state"] == "failing"
    assert build.check(REPO, "develop", ("Nothing",))["state"] == "unknown"


def findings(config):
    store = Store(config.db_path)
    try:
        return [dict(r) for r in store.db.execute("SELECT id, key, status, title FROM findings ORDER BY id")]
    finally:
        store.close()


def test_a_failing_workflow_becomes_one_finding_that_follows_it_and_is_marked_fixed_when_it_passes(monkeypatch, config):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(build, "pushed_to", lambda source: (REPO, "develop"))
    runs = []
    def get(url, text=False):
        if "/annotations" in url:
            return []
        if "/jobs" in url:
            return {"jobs": [{"id": 9, "name": "compile", "conclusion": "failure", "steps": []}]}
        return {"workflow_runs": runs}
    monkeypatch.setattr(build, "_get", get)

    runs[:] = [run(5, "Build", "failure"), run(4, "Scan")]
    assert build.record(config) == {"added": 1, "moved": 0, "fixed": 0}
    assert build.record(config) == {"added": 0, "moved": 0, "fixed": 0}  # the same failed run: nothing new
    first = findings(config)
    assert [(f["key"], f["status"]) for f in first] == [("build:ada/OBP-API:develop:Build:5", "open")]

    runs[:] = [run(6, "Build", "failure", sha="1234567890abc"), run(5, "Build", "failure")]
    assert build.record(config) == {"added": 0, "moved": 1, "fixed": 0}
    moved = findings(config)
    assert moved[0]["id"] == first[0]["id"] and moved[0]["key"].endswith(":Build:6") and "123456789" in moved[0]["title"]

    runs[:] = [run(7, "Build"), *runs]
    assert build.record(config) == {"added": 0, "moved": 0, "fixed": 1}
    assert findings(config)[0]["status"] == "fixed"

    runs[:] = [run(8, "Build", "failure"), *runs]  # broken again: a new finding
    assert build.record(config) == {"added": 1, "moved": 0, "fixed": 0}
    assert [f["status"] for f in findings(config)] == ["fixed", "open"]


def test_running_runs_change_nothing_and_no_checkout_means_no_build(monkeypatch, config):
    monkeypatch.setattr(build, "pushed_to", lambda source: (REPO, "develop"))
    fake(monkeypatch, [run(5, "Build", None, "in_progress")])
    assert build.record(config) == {"added": 0, "moved": 0, "fixed": 0}
    monkeypatch.setattr(build, "pushed_to", lambda source: None)
    assert build.record(config) == {"added": 0, "moved": 0, "fixed": 0} and build.status(config) is None


def test_one_instance_watches_each_build(monkeypatch, config):
    monkeypatch.setattr(build, "pushed_to", lambda source: (REPO, "develop") if source else None)
    a, b = replace(config, name="a", obp_api_source="/x"), replace(config, name="b", obp_api_source="/x")
    c = replace(config, name="c", obp_api_source="/x", build_workflows=("Build",))
    none = replace(config, name="d", obp_api_source="")
    assert [x.name for x in build.watched([a, b, c, none])] == ["a", "c"]
