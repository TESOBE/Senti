"""The `senti` source: Senti watching itself. It reads Senti's own warning and error lines from memory (logs.py keeps the latest as they are written), and the commit Senti runs from the checkout its code is in. It needs no network and no sign-in, so its instance has no credentials, and it is never in an OBP-API's app directory.

Only the `senti run` process that writes the lines can read them: run Senti's instance there, as every instance is.
"""

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from . import COMMIT, LOGS, Source
from .. import logs as senti_logs

if TYPE_CHECKING:
    from ..config import Config

CODE = Path(__file__).resolve().parent.parent  # src/senti: the code that runs


class SentiSource(Source):
    name = "senti"
    offers = frozenset({LOGS, COMMIT})

    def __init__(self, config: "Config", http=None):
        super().__init__(config)

    def logs(self, level: str, limit: int) -> list[str]:
        return senti_logs.recent.newest_first(level, limit)

    def running_commit(self) -> str | None:
        """The commit of the checkout Senti's code runs from, or None when it was installed without one."""
        try:
            out = subprocess.run(["git", "-C", str(CODE), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return out.stdout.strip() or None if out.returncode == 0 else None
