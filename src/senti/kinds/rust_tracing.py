"""A Rust service that logs with the tracing crate's JSON format (`tracing_subscriber::fmt().json()`).

One JSON object per line:

    {"timestamp":"2026-11-06T16:31:27.357523Z","level":"WARN","fields":{"message":"USD/fiat leg fell back",
     "currency":"KES","error":"HTTP status client error (401 Unauthorized)"},"target":"my_service::fx"}

The message is the template and the other fields are the values, so nothing has to be cut out of the text to tell two
lines of one problem apart. The fields go to the line's detail, `error` first, as `name=value` lines. The target (the
module) is the logger; there is no thread. A line that is not such an object (a panic, a library's output) is a plain
message. Fix this is not set up for this kind yet.
"""

import json
import re
from datetime import datetime

from . import NOTES, Kind, LogFormat, notes

NEVER = re.compile(r"(?!)")  # matches nothing: what is not a JSON line is a plain message


def structured(raw: str) -> dict | None:
    """The parts of one tracing JSON line, or None if the line is not one."""
    if not raw.startswith("{"):
        return None
    try:
        entry = json.loads(raw)
    except ValueError:
        return None
    fields = entry.get("fields") if isinstance(entry, dict) else None
    if not isinstance(fields, dict) or not isinstance(fields.get("message"), str) or "level" not in entry:
        return None
    values = {k: v for k, v in fields.items() if k != "message"}
    detail = "\n".join(f"{k}={values[k]}" for k in sorted(values, key=lambda k: k != "error"))
    return {"ts": str(entry.get("timestamp", "")), "logger": str(entry.get("target", "")),
            "message": fields["message"], "detail": detail}


LOG_FORMAT = LogFormat(line=NEVER, exceptions=(), path=NEVER, path_kept=0, structured=structured)

KIND = Kind("rust-tracing", __doc__.split("\n")[0], LOG_FORMAT, notes(NOTES / "rust-tracing.md"))
