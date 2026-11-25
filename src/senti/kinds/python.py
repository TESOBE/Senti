"""A Python service managed with uv that logs with the logging module, such as Senti itself.

Its lines are `%(asctime)s %(levelname)s %(name)s: %(message)s`, as Senti writes them, with the instance's URL in square brackets before the logger when the line is about one instance:

    2026-11-25 03:01:42,123 WARNING [http://localhost:8080] senti.collector: Polling log:error failed: ...

A logged exception's traceback follows on the next lines, and its last line names the exception (`httpx.ConnectError: ...`). Fix this checks a fix by compiling the code and running its pytest tests with uv.
"""

import re

from . import NOTES, Kind, LogFormat, Toolchain, in_folder, notes

LINE = re.compile(r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:[,.]\d+)?) (?P<level>[A-Z]+) "
                  r"(?:\[(?P<thread>[^\]\n]*)\] )?(?P<logger>[\w.]+): (?P<body>.*)", re.DOTALL)
# The last line of a traceback, `module.SomeError: message`, or an exception named in the message
EXCEPTIONS = (re.compile(r"^((?:[A-Za-z_]\w*\.)*[A-Z]\w*(?:Error|Exception|Exit|Interrupt|Timeout))(?::|$)", re.MULTILINE),
              re.compile(r"\b((?:[A-Za-z_]\w*\.)*[A-Z]\w*(?:Error|Exception))\b"))
PATH = re.compile(r"/api/[\w./{}-]+")
LOG_FORMAT = LogFormat(line=LINE, exceptions=EXCEPTIONS, path=PATH, path_kept=2)

FIX_NOTES = (
    "- Read {worktree}/CLAUDE.md first if it is there: it says how the code is written.\n"
    "- The code is in {worktree}/{folder}/src and its tests in {worktree}/{folder}/tests. A test is a pytest function "
    "in a tests/test_*.py file, named by what it shows.\n"
    "- Keep to the standard library and the packages pyproject.toml already lists: add none."
)


def uv(worktree: str, folder: str) -> list[str]:
    """uv run in the service's folder of the worktree, with the packages its pyproject.toml and uv.lock name."""
    return ["uv", "run", "--directory", in_folder(worktree, folder)]


TOOLCHAIN = Toolchain(
    build=uv,
    compile_goal=("python", "-m", "compileall", "-q", "src", "tests"),
    test_goal="pytest -q <test files, by path>",
    source_dirs=("{folder}/src", "{folder}/tests"),
    fix_notes=FIX_NOTES,
)

KIND = Kind("python", __doc__.split("\n")[0], LOG_FORMAT, notes(NOTES / "python.md"), toolchain=TOOLCHAIN)
