"""What the setup page (/setup) shows: where Senti's setup comes from, whether Claude Code can run, each service with
its checkout and whether Fix this works for it, and each instance with whether Senti is connected and what to do
about each problem.

Read from the setup, the file system and each instance's store: no calls to the services, OBP-API or Claude. Secrets
are never shown: an instance's OIDC client appears as the name of the setting that holds it.
"""

import shutil
from collections.abc import Callable
from pathlib import Path

from . import fixer
from .analyst import AGENT_FILE
from .config import Config, current_setup
from .health import health
from .home import home
from .kinds import kind_of
from .model import setup_path
from .store import Store


def _service(config: Config, instances: list[str]) -> dict:
    """A service as its first instance sees it: its checkout, and what Fix this needs."""
    toolchain = fixer.toolchain_of(config)
    source = config.obp_api_source
    # The build tool the toolchain runs (npm, mvn or the project's Maven wrapper), looked up as Senti would run it
    tool = toolchain.command(config.fix_worktree or source or ".")[0] if toolchain else None
    worktree = config.fix_worktree
    return {
        "name": config.service,
        "kind": config.kind,
        "kind_description": kind_of(config.kind).description,
        "repository": config.obp_api_github_repo,
        "checkout": source,
        "checkout_is_git": bool(source) and (Path(source) / ".git").exists(),
        "folder": config.folder,
        "fix_unavailable": fixer.unavailable_reason(config),
        "build_tool": Path(tool).name if tool else None,
        "build_tool_found": bool(tool and shutil.which(tool)),
        "worktree": worktree,
        "worktree_made": bool(worktree) and (Path(worktree) / ".git").exists(),
        "instances": instances,
    }


def _instance(store: Store, config: Config) -> dict:
    """An instance: where it is, how Senti signs in to it, and what stops Senti watching it."""
    h = health(store, config)
    last_poll = store.db.execute("SELECT MAX(ts) FROM polls WHERE ok = 1").fetchone()[0]
    return {
        "name": config.name,
        "service": config.service,
        "environment": config.environment,
        "parent": config.parent,
        "url": config.obp_base_url + config.monitoring_path,
        "reads_from": list(config.read_from),
        "oidc_issuer": config.oidc_issuer,
        "oidc_client_setting": config.oidc_client_setting,  # the setting's name, never its value
        "connected": h["connected"],
        "last_poll": last_poll,
        # Claude Code is shown once, at the top of the page, not under every instance
        "problems": [{k: p.get(k) for k in ("title", "action", "severity", "sources", "link")}
                     for p in h["problems"] if p["key"] != "analyst"],
        "more_may_follow": h["more_may_follow"],
    }


def status(configs: list[Config], with_store: Callable[[Config, Callable[[Store], dict]], dict]) -> dict:
    """Everything the setup page shows. `with_store` runs a function with an instance's store open."""
    setup = current_setup()
    first = configs[0]
    claude = shutil.which(first.claude_command)
    by_service: dict[str, list[Config]] = {}
    for c in configs:
        by_service.setdefault(c.service, []).append(c)
    return {
        "home": str(home()),
        "setup_file": setup.path if setup else None,
        "setup_file_expected": str(setup_path()),
        "env_file": str(home() / ".env"),
        "env_file_found": (home() / ".env").is_file(),
        "system": setup.system if setup else "",
        "ui": f"http://{first.ui_host}:{first.ui_port}",
        "claude": {
            "command": first.claude_command,
            "found": claude,
            "agent_file": str(home() / AGENT_FILE),
            "agent_file_found": (home() / AGENT_FILE).exists(),
            "budgets_usd": {"analysis": first.analyse_budget_usd, "source scan": first.scan_budget_usd,
                            "fix": first.fix_budget_usd},
            "analyse_minutes": first.analyse_minutes,
            # Every instance runs the same Claude Code: the latest check is kept with each, so read the first's
            "last_check": with_store(first, lambda s: s.get_state("claude_check")),
        },
        "services": [_service(cs[0], [c.name for c in cs]) for cs in by_service.values()],
        "instances": [with_store(c, lambda s, c=c: _instance(s, c)) for c in configs],
    }
