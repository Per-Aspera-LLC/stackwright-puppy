"""Tests for swp-erq2: 401 auth errors must be terminal, not silently retried.

Covers three layers:
1. copilot_auth/utils.py — exchange_for_session_token / get_valid_session_token
   now raise CopilotAuthRevokedError on a confirmed-401 (revoked token)
   instead of returning the same bare None used for every other failure.
2. agents/_runtime.py — should_retry_streaming treats ANY exception carrying
   status_code in (401, 403) as terminal, checked BEFORE snippet-text
   matching (closing the loophole where a coincidental snippet match could
   make an auth failure look retryable).
3. register_callbacks.py call sites — status/login/model-factory call sites
   catch CopilotAuthRevokedError and give a clear message; the live
   per-request auth_flow path deliberately does NOT catch it (lets it
   propagate to be classified terminal upstream).
"""

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import httpx
import pytest

from code_puppy.agents._runtime import should_retry_streaming
from code_puppy.plugins.copilot_auth.utils import (
    CopilotAuthRevokedError,
    exchange_for_session_token,
    get_valid_session_token,
)


# ---------------------------------------------------------------------------
# Layer 1: copilot_auth/utils.py
# ---------------------------------------------------------------------------


def _fake_response(status_code: int, json_body=None, text: str = ""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body or {}
    resp.text = text
    return resp


class TestExchangeForSessionToken:
    def test_401_raises_revoked_error_not_none(self):
        with patch(
            "code_puppy.plugins.copilot_auth.utils.requests.get",
            return_value=_fake_response(401),
        ):
            with pytest.raises(CopilotAuthRevokedError) as excinfo:
                exchange_for_session_token("ghp_fake", "github.com")
            assert excinfo.value.host == "github.com"
            assert excinfo.value.status_code == 401

    def test_timeout_still_returns_none(self):
        """Transient failures must keep returning None (worth retrying later)."""
        with patch(
            "code_puppy.plugins.copilot_auth.utils.requests.get",
            side_effect=httpx.TimeoutException("timed out"),
        ):
            # requests.exceptions.Timeout, not httpx -- use the real one
            import requests

            with patch(
                "code_puppy.plugins.copilot_auth.utils.requests.get",
                side_effect=requests.exceptions.Timeout("timed out"),
            ):
                result = exchange_for_session_token("ghp_fake", "github.com")
        assert result is None

    def test_500_still_returns_none(self):
        with patch(
            "code_puppy.plugins.copilot_auth.utils.requests.get",
            return_value=_fake_response(500, text="server error"),
        ):
            result = exchange_for_session_token("ghp_fake", "github.com")
        assert result is None

    def test_generic_exception_still_returns_none(self):
        with patch(
            "code_puppy.plugins.copilot_auth.utils.requests.get",
            side_effect=RuntimeError("network is down"),
        ):
            result = exchange_for_session_token("ghp_fake", "github.com")
        assert result is None

    def test_success_returns_session_token(self):
        with (
            patch(
                "code_puppy.plugins.copilot_auth.utils.requests.get",
                return_value=_fake_response(
                    200,
                    json_body={
                        "token": "sess_abc",
                        "expires_at": 9999999999,
                        "endpoints": {"api": "https://api.githubcopilot.com"},
                    },
                ),
            ),
            patch("code_puppy.plugins.copilot_auth.utils._persist_session"),
        ):
            result = exchange_for_session_token("ghp_fake", "github.com")
        assert result is not None
        assert result.token == "sess_abc"


class TestGetValidSessionToken:
    def test_propagates_revoked_error(self):
        """The terminal signal must survive the cache-then-exchange chain."""
        with (
            patch(
                "code_puppy.plugins.copilot_auth.utils._session_cache", {}
            ),
            patch(
                "code_puppy.plugins.copilot_auth.utils._load_persisted_session",
                return_value=None,
            ),
            patch(
                "code_puppy.plugins.copilot_auth.utils.exchange_for_session_token",
                side_effect=CopilotAuthRevokedError("github.com"),
            ),
        ):
            with pytest.raises(CopilotAuthRevokedError):
                get_valid_session_token("ghp_fake", "github.com")

    def test_transient_failure_still_returns_none(self):
        with (
            patch(
                "code_puppy.plugins.copilot_auth.utils._session_cache", {}
            ),
            patch(
                "code_puppy.plugins.copilot_auth.utils._load_persisted_session",
                return_value=None,
            ),
            patch(
                "code_puppy.plugins.copilot_auth.utils.exchange_for_session_token",
                return_value=None,
            ),
        ):
            result = get_valid_session_token("ghp_fake", "github.com")
        assert result is None


# ---------------------------------------------------------------------------
# Layer 2: agents/_runtime.py should_retry_streaming hardening
# ---------------------------------------------------------------------------


@dataclass
class _FakeStatusOnlyError(Exception):
    """A bare exception carrying only a `.status_code` attribute -- exercises
    the very first, type-agnostic short-circuit at the top of
    should_retry_streaming (before any ModelHTTPError/OpenAIAPIError
    isinstance check), which is what makes CopilotAuthRevokedError (a plain
    RuntimeError subclass) classify correctly with zero special-casing.
    """

    status_code: int
    message: str = "boom"

    def __str__(self) -> str:
        return self.message


def _model_http_error(status_code: int, message: str = "boom"):
    from pydantic_ai.exceptions import ModelHTTPError

    return ModelHTTPError(status_code=status_code, model_name="fake-model", body=message)


class TestShouldRetryStreamingAuthTerminal:
    def test_401_is_never_retryable_even_with_retryable_snippet_text(self):
        """The loophole this bead closes: a 401 whose message text happens
        to contain retryable-sounding boilerplate must still be terminal.
        """
        exc = _model_http_error(
            401, "internal server error, please retry your request"
        )
        assert should_retry_streaming(exc) is False

    def test_403_is_never_retryable(self):
        assert should_retry_streaming(_model_http_error(403, "forbidden")) is False

    def test_status_only_exception_401_is_terminal(self):
        """Any exception carrying status_code=401 is terminal, even one that
        isn't a recognized ModelHTTPError/OpenAIAPIError subclass at all --
        this is the exact shape CopilotAuthRevokedError relies on.
        """
        assert should_retry_streaming(_FakeStatusOnlyError(status_code=401)) is False

    def test_copilot_auth_revoked_error_is_terminal(self):
        """CopilotAuthRevokedError carries status_code=401 precisely so the
        SAME classifier picks it up with no copilot-specific special-casing.
        """
        assert should_retry_streaming(CopilotAuthRevokedError("github.com")) is False

    def test_429_still_retryable_unchanged(self):
        assert should_retry_streaming(_model_http_error(429, "rate limited")) is True

    def test_5xx_still_retryable_unchanged(self):
        assert (
            should_retry_streaming(_model_http_error(503, "service unavailable"))
            is True
        )

    def test_non_auth_exception_without_status_code_unaffected(self):
        assert should_retry_streaming(ValueError("plain bug")) is False
        assert should_retry_streaming(
            httpx.RemoteProtocolError("peer closed connection")
        ) is True


# ---------------------------------------------------------------------------
# Layer 3: register_callbacks.py call sites handle the terminal signal
# ---------------------------------------------------------------------------


class TestCopilotStatusHandlesRevokedToken:
    def test_status_reports_revoked_without_crashing(self):
        from code_puppy.plugins.copilot_auth.register_callbacks import (
            _handle_copilot_status,
        )

        fake_token = MagicMock(host="github.com", oauth_token="ghp_x", user="")

        with (
            patch(
                "code_puppy.plugins.copilot_auth.register_callbacks.load_device_tokens",
                return_value=[fake_token],
            ),
            patch(
                "code_puppy.plugins.copilot_auth.register_callbacks.get_valid_session_token",
                side_effect=CopilotAuthRevokedError("github.com"),
            ),
            patch(
                "code_puppy.plugins.copilot_auth.register_callbacks.load_copilot_models",
                return_value={},
            ),
            patch(
                "code_puppy.plugins.copilot_auth.register_callbacks.emit_warning"
            ) as mock_warning,
            patch("code_puppy.plugins.copilot_auth.register_callbacks.emit_success"),
            patch("code_puppy.plugins.copilot_auth.register_callbacks.emit_info"),
        ):
            # Must not raise -- the whole point is graceful, clear reporting.
            _handle_copilot_status()

        warning_texts = " ".join(str(c.args[0]) for c in mock_warning.call_args_list)
        assert "revoked" in warning_texts.lower()
