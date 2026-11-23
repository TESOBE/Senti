"""A Vue single-page app with an Express server.

API Explorer II writes plain messages; an error is `TypeError: message` followed by `    at ...` stack lines.

Fix this checks a fix with the app's own build (npm run build: the Vue client and the TypeScript server) and runs
the vitest files that cover it.
"""

from . import NOTES, Kind, Toolchain, notes, node
from ..packs.obp.kind import OBP_LOG_FORMAT  # until a general format exists: keeps the OBP apps' signature ids

FIX_NOTES = (
    "- Read {worktree}/{folder}/CLAUDE.md first if it is there: it says how the code is written. The Express server, "
    "where the problems in the logs come from, is in {worktree}/{folder}/server (routes, services); its tests are in "
    "{worktree}/{folder}/server/test. The Vue client is in {worktree}/{folder}/src.\n"
    "- A new check that fails returns the HTTP status that matches it (404 for something not found or not the "
    "caller's, 403 for a missing permission, 400 for bad input) with a plain message, never a stack trace.\n"
    "- A test is a vitest file (*.test.ts) next to the existing tests of that code; name test files by their path."
)

TOOLCHAIN = Toolchain(
    build=node.npm,
    compile_goal=("run", "build"),
    test_goal=node.TEST_GOAL,
    source_dirs=("{folder}/src", "{folder}/server", "{folder}/test"),
    fix_notes=FIX_NOTES,
    prepare=node.install,
)

KIND = Kind("vue-express", __doc__.split("\n")[0], OBP_LOG_FORMAT, notes(NOTES / "vue-express.md"), toolchain=TOOLCHAIN)
