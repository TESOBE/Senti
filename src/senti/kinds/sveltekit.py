"""A SvelteKit app on Node: server routes, sessions, calls to an API.

The OBP apps of this kind (the API Manager, the Portal, Cohort, the Sandbox Populator) write
`[timestamp] [LEVEL] [context] message`; an error is `TypeError: message` followed by `    at ...` stack lines.

Fix this checks a fix with the app's own build (npm run build), not svelte-check, which may report type errors the
app has already; Claude runs svelte-check too and adds no new errors. It runs the vitest files that cover the fix. In
a monorepo the app is its folder's npm workspace, and a fix may also change the shared code in packages/.
"""

from . import NOTES, Kind, Toolchain, notes, node
from ..packs.obp.kind import OBP_LOG_FORMAT  # until a general format exists: keeps the OBP apps' signature ids

FIX_NOTES = (
    "- Read {worktree}/CLAUDE.md and {worktree}/{folder}/CLAUDE.md first if they are there: they say how the code is "
    "written. The app's server code is in {worktree}/{folder}/src/routes (+page.server.ts, +layout.server.ts, "
    "+server.ts), {worktree}/{folder}/src/hooks.server.ts and {worktree}/{folder}/src/lib/server.\n"
    "- Change the shared code in {worktree}/packages only when the fix needs it there: every app uses it.\n"
    "- Also run svelte-check with the build command above followed by run check. The app may have type errors "
    "already: make sure your change adds none, in the files you changed.\n"
    "- A new check that fails returns the HTTP status that matches it (404 for something not found or not the "
    "caller's, 403 for a missing permission, 400 for bad input) with a plain message, never a stack trace.\n"
    "- A test is a vitest file (*.test.ts) next to the app's existing tests; name test files by their path."
)

TOOLCHAIN = Toolchain(
    build=node.npm,
    compile_goal=("run", "build"),
    test_goal=node.TEST_GOAL,
    source_dirs=("{folder}/src", "{folder}/test", "{folder}/tests", "packages"),
    fix_notes=FIX_NOTES,
    prepare=node.install,
)

KIND = Kind("sveltekit", __doc__.split("\n")[0], OBP_LOG_FORMAT, notes(NOTES / "sveltekit.md"), toolchain=TOOLCHAIN)
