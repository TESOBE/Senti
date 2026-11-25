"""What the person wants Senti to focus on for a service, in their own words (the Focus box on the page).

The focus is kept per service, in the database of each of its instances, so every instance of the service has it,
and the newest saved wins when they differ (an instance added after it was saved has none of its own).

Senti uses it in two ways:
- Every AI run on the service is told it (context.py): the analyst looks into what it names first, and marks each
  finding that falls within it `in_focus`.
- A finding in focus gets a higher priority (`store.FOCUS_WEIGHT`). Saving a new focus clears every finding's
  `in_focus`, because it was judged against the old one; the next analysis judges them again.
"""

from .config import Config
from .store import Store, now

FOCUS = "focus"  # state key: {"text": ..., "saved_at": ...}
MAX_LENGTH = 1000  # characters


def of_service(configs: list[Config], service: str) -> list[Config]:
    return [c for c in configs if c.service == service]


def get(configs: list[Config], service: str) -> str:
    """The focus of the service, or "" when none was saved."""
    saved = []
    for c in of_service(configs, service):
        store = Store(c.db_path)
        try:
            if value := store.get_state(FOCUS):
                saved.append(value)
        finally:
            store.close()
    return max(saved, key=lambda value: value["saved_at"])["text"] if saved else ""


def all_of(configs: list[Config]) -> dict[str, str]:
    """The focus of every service, by service name."""
    return {service: get(configs, service) for service in dict.fromkeys(c.service for c in configs)}


def save(configs: list[Config], service: str, text: str) -> str:
    """Save the focus of the service on each of its instances, and clear what was judged against the old one."""
    text = text.strip()
    if len(text) > MAX_LENGTH:
        raise ValueError(f"The focus can be at most {MAX_LENGTH} characters.")
    instances = of_service(configs, service)
    if not instances:
        raise ValueError(f"No service called {service}")
    value = {"text": text, "saved_at": now()}
    for c in instances:
        store = Store(c.db_path)
        try:
            store.set_state(FOCUS, value)
            store.clear_in_focus()
            store.commit()
        finally:
            store.close()
    return text
