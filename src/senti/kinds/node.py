"""What the Node kinds (sveltekit, vue-express) share for Fix this: npm, run for the service's folder, and its
packages installed in Senti's worktree from the lock file.

In an npm workspaces monorepo (OBP-Frontend: apps/<app>, packages/shared) the packages are installed once at the top
and the service is named with --workspace, so code in packages/ is the worktree's own, not the checkout's.
"""

from pathlib import Path

TEST_GOAL = "exec -- vitest run <test files, by path>"


def npm(worktree: str, folder: str) -> list[str]:
    """npm for the service: its workspace when it is in a folder, else the worktree."""
    return ["npm", "--prefix", worktree, *(["--workspace", folder] if folder else [])]


def install(worktree: str) -> list[str] | None:
    """npm ci when the worktree has no packages yet or its lock file changed since they were installed, else None."""
    lock, installed = Path(worktree) / "package-lock.json", Path(worktree) / "node_modules" / ".package-lock.json"
    if not lock.exists() or (installed.exists() and installed.stat().st_mtime >= lock.stat().st_mtime):
        return None
    return ["npm", "ci", "--prefer-offline", "--no-audit", "--no-fund"]

