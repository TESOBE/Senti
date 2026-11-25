"""Logging. Senti may watch several OBP-API instances, so every line names the one it is about, by its URL.

Each instance's work runs in its own threads (collector, analyst); a thread is tagged with its instance's URL
when it starts, and every line logged from it carries that URL.
"""

import logging
import threading
from collections import deque
from contextlib import contextmanager

FORMAT = "%(asctime)s %(levelname)s %(instance)s%(name)s: %(message)s"


def for_instance(thread: threading.Thread, obp_base_url: str) -> threading.Thread:
    """Tag `thread` (before starting it) so its log lines name `obp_base_url`."""
    thread.obp_base_url = obp_base_url
    return thread


@contextmanager
def about(obp_base_url: str):
    """Within this block, lines logged from the current thread name `obp_base_url` (for threads shared by
    all instances: the main thread, the web page's)."""
    thread = threading.current_thread()
    before = getattr(thread, "obp_base_url", None)
    thread.obp_base_url = obp_base_url
    try:
        yield
    finally:
        thread.obp_base_url = before


class InstanceFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        url = getattr(threading.current_thread(), "obp_base_url", None)
        record.instance = f"[{url}] " if url else ""
        return True


RECENT_LINES = 1000  # Senti's own warning and error lines kept for the senti source, per level


class RecentLines(logging.Handler):
    """Keeps Senti's latest warning and error lines in memory, as they are written, so the senti source can read them when Senti watches itself. A logged exception's line includes its traceback."""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.setFormatter(logging.Formatter(FORMAT))
        self.addFilter(InstanceFilter())
        self.lines = {"error": deque(maxlen=RECENT_LINES), "warning": deque(maxlen=RECENT_LINES)}

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:  # a line that cannot be formatted is not worth stopping for
            return
        self.lines["error" if record.levelno >= logging.ERROR else "warning"].append(line)

    def newest_first(self, level: str, limit: int) -> list[str]:
        """The latest lines of `level` (error or warning), newest first; none for another level."""
        lines = self.lines.get(level)
        return list(reversed(lines))[:limit] if lines else []


recent = RecentLines()


def setup(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format=FORMAT)
    for handler in logging.getLogger().handlers:
        handler.addFilter(InstanceFilter())
    logging.getLogger().addHandler(recent)
