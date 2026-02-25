"""Unit tests for free provider router."""

from __future__ import annotations

from drago.free_provider_router import (
    FreeProviderRouter,
    FreeProvidersExhaustedError,
    classify_provider_error,
)


def test_router_selects_first_available_provider(monkeypatch):
    monkeypatch.setenv("DRAGO_FREE_ONLY", "1")
    monkeypatch.setenv("DRAGO_FREE_PROVIDER_ORDER", "groq,openrouter,hf")
    monkeypatch.delenv("DRAGO_GROQ_API_KEY", raising=False)
    monkeypatch.setenv("DRAGO_OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.delenv("DRAGO_HF_TOKEN", raising=False)

    router = FreeProviderRouter()
    selection, event = router.select()

    assert selection.name == "openrouter"
    assert event is not None
    assert event.get("type") == "free_provider_selected"


def test_router_skips_exhausted_provider_with_cooldown(monkeypatch):
    monkeypatch.setenv("DRAGO_FREE_ONLY", "1")
    monkeypatch.setenv("DRAGO_FREE_PROVIDER_ORDER", "groq,openrouter")
    monkeypatch.setenv("DRAGO_FREE_PROVIDER_COOLDOWN_SEC", "600")
    monkeypatch.setenv("DRAGO_GROQ_API_KEY", "groq-key")
    monkeypatch.setenv("DRAGO_OPENROUTER_API_KEY", "or-key")

    router = FreeProviderRouter()
    first, _ = router.select()
    assert first.name == "groq"

    evt = router.mark_exhausted("groq", "quota_or_rate_limit")
    assert evt.get("type") == "free_provider_exhausted"

    second, switched = router.select()
    assert second.name == "openrouter"
    assert switched is not None
    assert switched.get("type") == "free_provider_switched"


def test_router_raises_when_no_provider_available(monkeypatch):
    monkeypatch.setenv("DRAGO_FREE_ONLY", "1")
    monkeypatch.setenv("DRAGO_FREE_PROVIDER_ORDER", "groq,hf")
    monkeypatch.delenv("DRAGO_GROQ_API_KEY", raising=False)
    monkeypatch.delenv("DRAGO_HF_TOKEN", raising=False)

    router = FreeProviderRouter()
    try:
        router.select()
        assert False, "Expected FreeProvidersExhaustedError"
    except FreeProvidersExhaustedError as e:
        assert isinstance(e.unavailable_reasons, dict)
        assert "groq" in e.unavailable_reasons
        assert "hf" in e.unavailable_reasons


def test_classify_provider_error_detects_quota_by_status():
    class _Err(Exception):
        status_code = 429

    error_class, is_quota = classify_provider_error(_Err("429"))
    assert error_class == "quota_or_rate_limit"
    assert is_quota is True


def test_classify_provider_error_detects_quota_by_text():
    error_class, is_quota = classify_provider_error(RuntimeError("rate limit exceeded"))
    assert error_class == "quota_or_rate_limit"
    assert is_quota is True


def test_classify_provider_error_handles_non_numeric_status_code():
    class _Err(Exception):
        status_code = "n/a"

    error_class, is_quota = classify_provider_error(_Err("invalid api key"))
    assert error_class == "auth_error"
    assert is_quota is False
