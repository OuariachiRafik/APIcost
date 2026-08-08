# ADR 0008 — Redis checkpointing lives outside `stats/`

**Status:** accepted · **Date:** 2026-08-08 · **Phase:** P6

## Context

Two authoritative documents disagree.

BUILD_SPEC §3 lays out `stats/rolling.py` as "windowed state, **Redis checkpointing**", and §6.5
repeats it: "State checkpointed to Redis and persisted to `rolling_stats`."

CLAUDE.md §Style says: "Keep `metrics/`, `stats/`, and `advisor/breakeven.py` pure — **no I/O, no ORM
imports**." `apicost.stats` is also a `mypy --strict` target.

Both cannot hold. A module that reads Redis and writes Postgres is not pure.

## Decision

CLAUDE.md wins, because its own preamble says its instructions override, and because the constraint
it is protecting is the more valuable of the two: a statistics core with no I/O is one that can be
tested exhaustively without a database, and the Welford tests in
`tests/unit/test_stats_and_anomaly.py` are the whole reason the numerical edge cases are pinned at
all.

So the split is by *responsibility*, not by subject:

- `stats/welford.py`, `stats/rolling.py` — pure state machines. What a baseline is, how a window
  advances, how a checkpoint deserialises. No imports outside the standard library and each other.
- `anomaly/store.py` — where that state lives. Redis working copy, Postgres durable copy.

This is a deviation from BUILD_SPEC §3's file layout and is recorded here rather than made silently,
per CLAUDE.md §How to work here.

## Consequences

- `stats/` stays trivially testable and strictly typed.
- One extra module, and one more hop to follow when reading the checkpoint path. `stats/rolling.py`
  says in its docstring where its I/O went, so the trail is not cold.
- Anyone diffing the tree against BUILD_SPEC §3 will find `stats/rolling.py` smaller than described
  and an `anomaly/store.py` that the spec does not mention. That is this ADR.
