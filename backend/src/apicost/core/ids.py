"""Identifier generation.

Every primary key in the system is a ULID stored as text (BUILD_SPEC §7):
lexicographically sortable by creation time, and safe to expose to users
because it leaks nothing but a timestamp.
"""

from __future__ import annotations

from ulid import ULID

__all__ = ["new_id", "new_request_id"]


def new_id() -> str:
    """Return a fresh ULID as a 26-character canonical string."""
    return str(ULID())


def new_request_id() -> str:
    """Return the identifier that traces one request end to end.

    The same value is bound to the logging context, written to the ledger, and
    returned to the caller in ``X-APICost-Request-Id`` (CODEBASE_GUIDE §8.5).
    """
    return new_id()
