"""ULID identifiers — BUILD_SPEC §7."""

from __future__ import annotations

import re
import time

from apicost.core.ids import new_id, new_request_id

CROCKFORD_ULID = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")


def test_shape_is_canonical_ulid() -> None:
    value = new_id()
    assert len(value) == 26
    assert CROCKFORD_ULID.match(value), value


def test_ids_are_unique() -> None:
    assert len({new_id() for _ in range(10_000)}) == 10_000


def test_ids_sort_by_creation_time() -> None:
    """Sortability is the reason these are ULIDs and not UUID4s."""
    first = new_id()
    time.sleep(0.002)
    second = new_id()
    assert first < second


def test_request_id_is_a_ulid() -> None:
    assert CROCKFORD_ULID.match(new_request_id())
