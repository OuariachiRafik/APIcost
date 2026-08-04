# ADR 0001 — uv as the Python package *and* interpreter manager

**Status:** accepted · **Date:** 2026-08-04 · **Phase:** P0

## Context

BUILD_SPEC §2 locks the backend to **Python 3.12**. It does not name a package manager, so P0 had to
pick one, and the choice is constrained by what the development machine actually looks like:

- `python3` on PATH is a **conda 3.13** interpreter, which shadows the system Python.
- `/usr/bin/python3.12` exists but has neither `pip` nor `venv` (`python3.12-venv` is not installed),
  so it cannot bootstrap an environment on its own.
- Installing system packages requires `sudo`, which should not be a prerequisite for building the
  project.

Any workflow that says "run `python3 -m venv`" or "`pip install -r requirements.txt`" silently
resolves to 3.13 here. That is not a hypothetical: it produces an environment one minor version off
the locked target, and the failure shows up later as a dependency resolving differently in CI than on
the machine.

## Decision

Use **uv** for both dependency management and interpreter provisioning.

- `backend/.python-version` pins `3.12`. `uv` downloads and manages that interpreter itself, in its
  own store, ignoring conda and the system Python entirely. No `sudo`, no PATH surgery.
- `backend/pyproject.toml` declares dependencies with `requires-python = ">=3.12,<3.13"` and PEP 735
  `[dependency-groups]` for `dev` and `ml`, satisfying the "dependency groups" requirement in §4 P0.
- `backend/uv.lock` is committed. It is the single resolution shared by developers, CI, and the
  Docker images.
- **Every** Python invocation goes through `uv run --project backend`: the `Makefile`, the CI
  workflow, and Alembic. A bare `python`/`python3`/`pip` anywhere in this repo is a bug — it means
  something is running on whichever interpreter happened to be first on PATH.
- The backend image is built `FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim` and installs with
  `uv sync --frozen`, so the container interpreter and the lockfile match local development exactly.

## Consequences

- Contributors need `uv` (`curl -LsSf https://astral.sh/uv/install.sh | sh`). They do **not** need a
  system Python 3.12, `pip`, `virtualenv`, or root.
- `uv.lock` must be regenerated and committed whenever `pyproject.toml` changes. CI installs with
  `--frozen`, so a stale lockfile fails the build rather than silently resolving something new.
- This does not alter any locked decision in §2. It is the mechanism by which the locked Python 3.12
  is actually obtained.
- The `ml` group (fastembed, scikit-learn) is excluded from images that do not need it. When the
  embedder lands in P4, the proxy image gains `--group ml`; the API image should not.
