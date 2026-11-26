"""A service's build on GitHub Actions: the workflows of the repository its checkout pushes to, on the branch it
pushes to (git's @{push}; without an upstream, the remote git would push the current branch to). With `workflows` in the
service's senti.toml entry only those workflows count; without it, all of them. A service with no checkout, or whose
checkout pushes somewhere other than GitHub, has no build.

The latest run of each workflow counted: OK when all of them passed (or were skipped), failing when one failed, with
which workflow, at which commit, and which job and step, running while one is still going. Shown at the top of the
page, asked of GitHub at most every CHECK_SECONDS, shared by the instances whose checkouts push to the same place.

Senti also checks by itself, every WATCH_SECONDS while `senti run` runs (watch_forever): a workflow whose latest run
failed becomes a finding, "Fix the build", in the first instance that watches that build. While the workflow keeps
failing, the same finding follows its latest failed run. When its latest run passes, the finding is marked fixed,
unless the person has already acted on it or dismissed it. If it fails again later, that is a new finding.

GITHUB_TOKEN, if set, is sent along: for a private repository, a higher rate limit, and the end of a failed job's log.
Without it GitHub allows 60 requests an hour, which is why Senti checks by itself only every quarter of an hour.
"""

import logging
import os
import re
import subprocess
import threading
import time

import httpx

from .config import Config
from .store import Store

logger = logging.getLogger(__name__)

CHECK_SECONDS = 300  # the page's view of the build is at most this old
WATCH_SECONDS = 900  # how often Senti checks by itself
FIRST_WATCH_SECONDS = 60  # the first check, soon after Senti starts
GITHUB_REMOTE = re.compile(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$")
PASSED = {"success", "skipped", "neutral"}
STILL_OPEN = ("open", "suggested", "accepted", "later")  # a build finding Senti may still move on or mark fixed
KEY_PREFIX = "build:"
LOG_LINES = 150  # Fix the build: the end of each failed job's log
DETAILS_MAX = 12000
_cache: dict[tuple, tuple[float, dict]] = {}
_lock = threading.Lock()


def _get(url: str, text: bool = False) -> dict | str:
    headers = {"Accept": "application/vnd.github+json"}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    response = httpx.get(url, headers=headers, timeout=30 if text else 10, follow_redirects=text)
    response.raise_for_status()
    return response.text if text else response.json()


def _git(source: str, *args: str) -> str:
    out = subprocess.run(["git", "-C", source, *args], capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else ""


def pushed_to(source: str) -> tuple[str, str | None] | None:
    """(owner/repo, branch): the GitHub repository and branch the checkout pushes to; None if it pushes elsewhere or
    there is no checkout. The branch is None on a detached HEAD: then every branch's runs count."""
    if not source:
        return None
    push = _git(source, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{push}")  # e.g. origin/develop
    remote, _, branch = push.partition("/")
    if not push:  # no upstream yet: the remote git would push to, by its own order, and the current branch
        branch = _git(source, "symbolic-ref", "--short", "-q", "HEAD")  # "" on a detached HEAD
        remote = next((r for r in (_git(source, "config", f"branch.{branch}.pushRemote") if branch else "",
                                   _git(source, "config", "remote.pushDefault"),
                                   _git(source, "config", f"branch.{branch}.remote") if branch else "") if r),
                      "origin")
    m = GITHUB_REMOTE.search(_git(source, "remote", "get-url", "--push", remote))
    return (f"{m['owner']}/{m['repo']}", branch or None) if m else None


def actions_page(repo: str) -> str:
    return f"https://github.com/{repo}/actions"


def _latest(runs: list[dict], workflows: tuple[str, ...] = ()) -> dict:
    """The latest run of each workflow counted, by workflow name."""
    latest = {}
    for run in runs:  # newest first: the first run of each workflow is its latest
        if not workflows or run["name"] in workflows:
            latest.setdefault(run["name"], run)
    return latest


def _failed(latest: dict) -> list[dict]:
    return [r for r in latest.values() if r["status"] == "completed" and r["conclusion"] not in PASSED]


def latest_runs(repo: str, branch: str | None, workflows: tuple[str, ...] = ()) -> dict:
    runs = _get(f"https://api.github.com/repos/{repo}/actions/runs?per_page=30"
                + (f"&branch={branch}" if branch else ""))["workflow_runs"]
    return _latest(runs, workflows)


def status(config: Config) -> dict | None:
    """{state: ok | failing | running | unknown, text, url}, or None when the service has no build."""
    where = pushed_to(config.obp_api_source)
    if not where:
        return None
    key = (*where, config.build_workflows)
    with _lock:
        cached = _cache.get(key)
        if cached and time.time() - cached[0] < CHECK_SECONDS:
            return cached[1]
    shown = check(*where, config.build_workflows)
    with _lock:
        _cache[key] = (time.time(), shown)
    return shown


def check(repo: str, branch: str | None, workflows: tuple[str, ...] = ()) -> dict:
    url = actions_page(repo)
    on = f" on {branch}" if branch else ""
    try:
        latest = latest_runs(repo, branch, workflows)
        if not latest:
            named = f" of {', '.join(workflows)}" if workflows else ""
            return {"state": "unknown", "text": f"No GitHub Actions runs{named} in {repo}{on} yet", "url": url}
        failed = _failed(latest)
        going = [r for r in latest.values() if r["status"] != "completed"]
        if failed:
            run = failed[0]
            jobs = _get(f"https://api.github.com/repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100")["jobs"]
            why = [f"job {j['name']} failed" + (f" at step {s['name']}" if (s := next(
                (s for s in j.get("steps", []) if s.get("conclusion") == "failure"), None)) else "")
                   for j in jobs if j.get("conclusion") not in PASSED | {None}]
            return {"state": "failing", "url": run["html_url"],
                    "text": f"{run['name']} {run['conclusion'].replace('_', ' ')} at {run['head_sha'][:9]} in {repo}{on}"
                            + (f": {'; '.join(why)}" if why else "")}
        newest = max(latest.values(), key=lambda r: r["created_at"])
        if going:
            return {"state": "running", "url": going[0]["html_url"],
                    "text": f"{going[0]['name']} is running at {going[0]['head_sha'][:9]} in {repo}{on}; the others passed"}
        return {"state": "ok", "url": newest["html_url"],
                "text": f"Build OK: the latest run of each workflow in {repo}{on} passed, up to {newest['head_sha'][:9]}"}
    except (httpx.HTTPError, KeyError, ValueError) as e:
        logger.warning("Could not read the build of %s: %s", repo, e)
        return {"state": "unknown", "text": f"Could not read the build from GitHub: {e}"[:300], "url": url}


def _details(repo: str, run: dict) -> str:
    """The failed jobs and steps of a run, GitHub's error annotations and (with GITHUB_TOKEN, which job logs need)
    the end of each failed job's log."""
    api = f"https://api.github.com/repos/{repo}"
    details = []
    for job in _get(f"{api}/actions/runs/{run['id']}/jobs?per_page=100")["jobs"]:
        if job.get("conclusion") in PASSED | {None}:
            continue
        steps = [s["name"] for s in job.get("steps", []) if s.get("conclusion") == "failure"]
        details.append(f"Job {job['name']} failed" + (f" at step {', '.join(steps)}" if steps else "") + ".")
        try:  # a job is a check run: its annotations are the errors GitHub picked out
            notes = _get(f"{api}/check-runs/{job['id']}/annotations?per_page=50")
            details += [f"- {a.get('path') or ''}:{a.get('start_line') or ''} {a.get('message', '').strip()}"[:600]
                        for a in notes if a.get("annotation_level") == "failure"]
        except httpx.HTTPError as e:
            logger.warning("Could not read the annotations of job %s: %s", job["id"], e)
        if os.environ.get("GITHUB_TOKEN"):
            try:
                tail = _get(f"{api}/actions/jobs/{job['id']}/logs", text=True).splitlines()[-LOG_LINES:]
                details.append("The end of its log:\n" + "\n".join(tail))
            except httpx.HTTPError as e:
                logger.warning("Could not read the log of job %s: %s", job["id"], e)
    return "\n".join(details)[-DETAILS_MAX:]


def _workflow_prefix(repo: str, branch: str | None, workflow: str) -> str:
    """What the keys of one workflow's build findings start with; the failed run's id follows."""
    return f"{KEY_PREFIX}{repo}:{branch or ''}:{workflow}:"


def failure(config: Config) -> dict | None:
    """What failed in the latest failing run, for Fix the build: {key, run_id, name, sha, url, repo, branch, details}.
    None when no workflow is failing. Asked of GitHub now, not cached."""
    where = pushed_to(config.obp_api_source)
    if not where:
        return None
    repo, branch = where
    failed = _failed(latest_runs(repo, branch, config.build_workflows))
    if not failed:
        return None
    return _failure(repo, branch, failed[0])


def _failure(repo: str, branch: str | None, run: dict) -> dict:
    return {"key": _workflow_prefix(repo, branch, run["name"]) + str(run["id"]), "run_id": run["id"],
            "name": run["name"], "sha": run["head_sha"], "url": run["html_url"], "repo": repo, "branch": branch,
            "details": _details(repo, run)}


def as_finding(failed: dict) -> dict:
    """A failing run as a finding."""
    on = f" on {failed['branch']}" if failed.get("branch") else ""
    return {
        "key": failed["key"], "category": "bug",
        "title": f"Fix the build: {failed['name']} fails at {failed['sha'][:9]}",
        "summary": f"The {failed['name']} workflow on GitHub Actions fails at {failed['sha'][:9]} in "
                   f"{failed.get('repo', 'the repository')}{on}.",
        "evidence": f"{failed['url']}\n{failed['details']}",
        "hypothesis": "See the failed jobs, steps and log above.",
        "files": [], "signature_ids": [],
        "suggested_fix": "Find from the failure above why the build fails (a compile error or a failing test), and "
                         "change the code, or the test if the test is wrong, so it passes.",
        "impact": 5, "confidence": 1, "effort": 2, "trend": "new"}


def _still_open(store: Store, prefix: str) -> list:
    return store.db.execute(
        f"SELECT id, key FROM findings WHERE substr(key, 1, ?) = ? AND status IN ({', '.join('?' * len(STILL_OPEN))})",
        (len(prefix), prefix, *STILL_OPEN)).fetchall()


def record(config: Config) -> dict:
    """Bring the build findings up to date with the latest run of each workflow counted: a failing workflow's
    finding is added or follows its latest failed run; a passing workflow's open finding is marked fixed.
    Returns how many were added, moved on to a later run and marked fixed (all 0 if there is no build)."""
    where = pushed_to(config.obp_api_source)
    if not where:
        return {"added": 0, "moved": 0, "fixed": 0}
    repo, branch = where
    latest = latest_runs(repo, branch, config.build_workflows)
    done = {"added": 0, "moved": 0, "fixed": 0}
    store = Store(config.db_path)
    try:
        for name, run in latest.items():
            if run["status"] != "completed":
                continue
            prefix = _workflow_prefix(repo, branch, name)
            still = _still_open(store, prefix)
            if run["conclusion"] in PASSED:
                for row in still:
                    store.add_feedback(row["id"], "fixed", f"{name} passes again at {run['head_sha'][:9]}.", 0,
                                       from_sentinel=True)
                    done["fixed"] += 1
                continue
            key = prefix + str(run["id"])
            if store.db.execute("SELECT 1 FROM findings WHERE key = ?", (key,)).fetchone():
                continue  # this failed run is a finding already
            if still:  # the workflow still fails, at a later run: its finding follows it
                store.db.execute("UPDATE findings SET key = ? WHERE id = ?", (key, still[0]["id"]))
                done["moved"] += 1
            else:
                done["added"] += 1
            store.upsert_finding(as_finding(_failure(repo, branch, run)))
        store.commit()
    finally:
        store.close()
    return done


def watched(configs: list[Config]) -> list[Config]:
    """For each build (repository, branch and workflows counted), the first instance that watches it."""
    seen, chosen = set(), []
    for c in configs:
        where = pushed_to(c.obp_api_source)
        if where and (key := (*where, c.build_workflows)) not in seen:
            seen.add(key)
            chosen.append(c)
    return chosen


def watch_forever(configs: list[Config], stopped: threading.Event) -> None:
    delay = FIRST_WATCH_SECONDS
    while not stopped.wait(delay):
        delay = WATCH_SECONDS
        for config in watched(configs):  # read again each time: a checkout may have moved to another branch
            try:
                done = record(config)
                if any(done.values()):
                    logger.info("Build of %s (%s): %s", pushed_to(config.obp_api_source), config.name, done)
            except (httpx.HTTPError, KeyError, ValueError) as e:
                logger.warning("Could not read the build of %s: %s", config.name, e)
