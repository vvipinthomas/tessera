"""What an error may tell the person who caused it.

Tessera's own errors (a query that is not allowed, an unknown column in an OData filter, a connection that failed its
test) are written for people and subclass UserError. Anything else, such as a library or driver exception, may carry
internal detail (file paths, hostnames, stack frames), so people see a generic message and the details go to the log.
"""
from __future__ import annotations

import logging

log = logging.getLogger("tessera")

GENERIC = "Something went wrong. The details are in the server log."


class UserError(Exception):
    """An error whose message was written to be shown to the person using Tessera."""


def public_message(e: BaseException, also: tuple[type[BaseException], ...] = (), limit: int = 500) -> str:
    """The text that may be shown for e. `also` admits more exception types whose messages are known to be safe in
    that place (for example ValueError raised by Tessera's own agent tools)."""
    if isinstance(e, (UserError, *also)):
        text = str(e.args[0]) if e.args else e.__class__.__name__
        return text.strip().strip("'").splitlines()[0][:limit] if text.strip() else e.__class__.__name__
    log.warning("unexpected error shown generically: %s", e.__class__.__name__, exc_info=e)
    return GENERIC
