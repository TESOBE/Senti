"""A Scala service on http4s and cats-effect, built with Maven.

OBP-OIDC writes `logger - message` or plain messages, an exception followed by its `\tat ...` stack lines.

Fix this builds offline with Maven (the packages come from ~/.m2) and runs the ScalaTest suites that cover a fix.
"""

from . import NOTES, Kind, Toolchain, in_folder, notes
from ..packs.obp.kind import OBP_LOG_FORMAT  # until a general format exists: keeps the OBP apps' signature ids


def maven(worktree: str, folder: str = "") -> list[str]:
    """Offline Maven for the service's pom.xml."""
    return ["mvn", "-o", "-q", "-f", f"{in_folder(worktree, folder)}/pom.xml"]


FIX_NOTES = (
    "- Read {worktree}/{folder}/CLAUDE.md first if it is there: it says how the code is written. The code is in "
    "{worktree}/{folder}/src/main/scala: routes are http4s HttpRoutes, effects are cats-effect IO.\n"
    "- A new check that fails returns the HTTP status that matches it (404 for something not found or not the "
    "caller's, 403 for a missing permission, 400 for bad input, and the OAuth 2 error responses where the "
    "specification says so) with a plain message, never a stack trace.\n"
    "- A test is a ScalaTest suite under {worktree}/{folder}/src/test/scala. Name suites by fully qualified class name "
    "(grep the test sources for the class)."
)

TOOLCHAIN = Toolchain(
    build=maven,
    compile_goal=("test-compile",),
    test_goal="test -Dsuites=<Suite1,Suite2>",
    source_dirs=("{folder}/src",),
    fix_notes=FIX_NOTES,
)

KIND = Kind("scala-http4s", __doc__.split("\n")[0], OBP_LOG_FORMAT, notes(NOTES / "scala-http4s.md"),
            toolchain=TOOLCHAIN)
