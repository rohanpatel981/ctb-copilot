"""Tests for the bearer-token verifier. These exercise the dependency
function directly (not through the full FastAPI app) so we don't need
the Anthropic SDK or DocumentDB mocked."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from ctb_copilot.config import settings


def _call_verify(authorization: str | None, token: str | None) -> None:
    """Import verify_token + invoke with a temporarily-patched token.
    Importing api.py at module scope would require ANTHROPIC_API_KEY +
    a DuckDB; doing it here keeps the tests environment-clean."""
    from ctb_copilot.api import verify_token

    original = settings.api_token
    settings.api_token = token
    try:
        verify_token(authorization=authorization)
    finally:
        settings.api_token = original


def test_no_token_configured_means_no_auth_dev_mode() -> None:
    # Dev mode: settings.api_token is None → header is ignored entirely
    _call_verify(authorization=None, token=None)
    _call_verify(authorization="literally anything", token=None)


def test_missing_authorization_header_rejected_when_token_set() -> None:
    with pytest.raises(HTTPException) as exc:
        _call_verify(authorization=None, token="s3cret")
    assert exc.value.status_code == 401
    assert "Bearer" in exc.value.detail


def test_non_bearer_scheme_rejected() -> None:
    with pytest.raises(HTTPException) as exc:
        _call_verify(authorization="Basic dXNlcjpwYXNz", token="s3cret")
    assert exc.value.status_code == 401


def test_bearer_with_wrong_token_rejected() -> None:
    with pytest.raises(HTTPException) as exc:
        _call_verify(authorization="Bearer wrong-token", token="s3cret")
    assert exc.value.status_code == 401
    assert "Invalid" in exc.value.detail


def test_bearer_with_correct_token_accepted() -> None:
    # No exception raised
    _call_verify(authorization="Bearer s3cret", token="s3cret")


def test_bearer_case_insensitive_on_scheme() -> None:
    # `bearer foo` should also work — case-insensitive scheme
    _call_verify(authorization="bearer s3cret", token="s3cret")
    _call_verify(authorization="BEARER s3cret", token="s3cret")


def test_compare_digest_is_used_for_constant_time_comparison() -> None:
    """Smoke-test that we don't `==` raw — wrong-but-close-length tokens
    should still raise, and the rejection is timing-attack-resistant.
    We can't really test timing in unit tests, but at least verify
    the basic behaviour."""
    for wrong in ("s3cre", "s3cretX", "S3CRET", "s3cret_"):
        with pytest.raises(HTTPException):
            _call_verify(authorization=f"Bearer {wrong}", token="s3cret")
