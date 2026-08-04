"""Secret redaction — CLAUDE.md hard rule 3, BUILD_SPEC §0.2.

This is a security control, so the tests are adversarial rather than
illustrative: nested structures, exception messages, rendered tracebacks, and
the stdlib path that third-party libraries log through.
"""

from __future__ import annotations

import json
import logging
import sys

import pytest

from apicost.core.logging import (
    REDACTION_PLACEHOLDER,
    SecretRedactionFilter,
    bind_request_id,
    clear_request_id,
    configure_logging,
    get_logger,
    get_request_id,
    redact,
    redact_text,
    reset_request_id,
)

OPENAI_KEY = "sk-proj-A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
ANTHROPIC_KEY = "sk-ant-api03-Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2"
PROXY_KEY = "apc_live_9f8e7d6c5b4a3210ZYXWVUTSRQPO"

ALL_KEYS = (OPENAI_KEY, ANTHROPIC_KEY, PROXY_KEY)


@pytest.mark.parametrize("key", ALL_KEYS)
def test_each_key_shape_is_redacted(key: str) -> None:
    redacted = redact_text(f"forwarding with {key} now")
    assert key not in redacted
    assert REDACTION_PLACEHOLDER in redacted


def test_anthropic_prefix_is_not_mangled_by_the_openai_pattern() -> None:
    """`sk-` must not win over `sk-ant-` and leave a misleading marker."""
    redacted = redact_text(ANTHROPIC_KEY)
    assert redacted.startswith("sk-ant-")
    assert "api03" not in redacted


def test_scheme_prefix_is_preserved_for_diagnosis() -> None:
    assert redact_text(OPENAI_KEY).startswith("sk-")
    assert redact_text(PROXY_KEY).startswith("apc_live_")


def test_multiple_keys_in_one_string() -> None:
    text = f"{OPENAI_KEY} then {ANTHROPIC_KEY} then {PROXY_KEY}"
    redacted = redact_text(text)
    for key in ALL_KEYS:
        assert key not in redacted


def test_nested_structures_are_redacted() -> None:
    payload = {
        "headers": {"authorization": f"Bearer {PROXY_KEY}"},
        "attempts": [{"provider_key": OPENAI_KEY}, ANTHROPIC_KEY],
        "tags": {f"key:{OPENAI_KEY}"},
        "pair": (ANTHROPIC_KEY, 42),
    }
    rendered = repr(redact(payload))
    for key in ALL_KEYS:
        assert key not in rendered


def test_exception_messages_are_redacted() -> None:
    error = ValueError(f"auth failed for {OPENAI_KEY}")
    assert OPENAI_KEY not in str(redact(error))


def test_non_string_values_pass_through_unchanged() -> None:
    assert redact(42) == 42
    assert redact(None) is None
    assert redact(3.5) == 3.5


@pytest.mark.parametrize("key", ALL_KEYS)
def test_redaction_is_idempotent(key: str) -> None:
    """A record can pass through several filters; redacting twice must not differ.

    Without this, "apc_live_***REDACTED***" re-matches as "apc_" + "live_" and
    accumulates placeholders on every pass.
    """
    once = redact_text(f"Bearer {key}")
    assert redact_text(once) == once
    assert once.count(REDACTION_PLACEHOLDER) == 1


def test_short_lookalikes_are_left_alone() -> None:
    """Redaction must not chew up ordinary prose containing 'sk-'."""
    assert redact_text("sk-1") == "sk-1"
    assert redact_text("ask-me-later") == "ask-me-later"


def test_rendered_log_line_contains_no_secret(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", json_output=True, service="test")
    logger = get_logger("test")

    logger.info(
        "provider_forward",
        provider_key=OPENAI_KEY,
        detail={"authorization": f"Bearer {PROXY_KEY}"},
    )

    captured = capsys.readouterr().out
    for key in (OPENAI_KEY, PROXY_KEY):
        assert key not in captured
    assert REDACTION_PLACEHOLDER in captured
    assert json.loads(captured.strip())["event"] == "provider_forward"


def test_rendered_traceback_contains_no_secret(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """format_exc_info runs before redaction, so tracebacks are scrubbed too."""
    configure_logging(level="INFO", json_output=True, service="test")
    logger = get_logger("test")

    try:
        raise RuntimeError(f"upstream rejected {ANTHROPIC_KEY}")
    except RuntimeError:
        logger.exception("forward_failed")

    captured = capsys.readouterr().out
    assert ANTHROPIC_KEY not in captured
    assert REDACTION_PLACEHOLDER in captured


def test_stdlib_filter_redacts_message_and_args() -> None:
    """uvicorn, httpx, and SQLAlchemy log through the stdlib, not structlog."""
    filt = SecretRedactionFilter()
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="auth header %s for %s",
        args=(PROXY_KEY, OPENAI_KEY),
        exc_info=None,
    )

    assert filt.filter(record) is True
    rendered = record.getMessage()
    assert PROXY_KEY not in rendered
    assert OPENAI_KEY not in rendered


def test_uvicorn_access_log_is_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    """uvicorn installs its own handlers and does not propagate to root.

    Its access log renders the request line verbatim, so a proxy key in a query
    string would otherwise be written out in the clear.
    """
    access = logging.getLogger("uvicorn.access")
    access.propagate = False
    handler = logging.StreamHandler(stream=sys.stdout)
    access.addHandler(handler)
    access.setLevel(logging.INFO)

    try:
        configure_logging(level="INFO", json_output=True, service="proxy")
        access.info('127.0.0.1 - "GET /v1/chat?key=%s HTTP/1.1" 200', PROXY_KEY)
        captured = capsys.readouterr().out
    finally:
        access.removeHandler(handler)
        access.filters.clear()

    assert PROXY_KEY not in captured
    assert REDACTION_PLACEHOLDER in captured


def test_stdlib_filter_handles_dict_args() -> None:
    filt = SecretRedactionFilter()
    record = logging.LogRecord(
        name="lib",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="key=%(key)s",
        args=({"key": OPENAI_KEY},),
        exc_info=None,
    )

    assert filt.filter(record) is True
    assert OPENAI_KEY not in record.getMessage()


# ---------------------------------------------------------------------------
# Request-id context
# ---------------------------------------------------------------------------


def test_request_id_binds_and_resets() -> None:
    assert get_request_id() is None

    token = bind_request_id("01JABCDEF0123456789ABCDEFG")
    assert get_request_id() == "01JABCDEF0123456789ABCDEFG"

    reset_request_id(token)
    assert get_request_id() is None


def test_request_id_appears_in_output(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", json_output=True, service="test")
    token = bind_request_id("01JREQUESTID0000000000000")
    try:
        get_logger("test").info("handled")
    finally:
        reset_request_id(token)

    line = json.loads(capsys.readouterr().out.strip())
    assert line["request_id"] == "01JREQUESTID0000000000000"


def test_clear_request_id() -> None:
    bind_request_id("01JSOMETHING00000000000000")
    clear_request_id()
    assert get_request_id() is None
