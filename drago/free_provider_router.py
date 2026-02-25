"""
Free provider router for DRAGO_FREE_ONLY mode.

Selects available free providers/models, applies cooldown on quota/rate-limit
errors, and exposes structured events for observability.
"""

from __future__ import annotations

import datetime
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _parse_int(name: str, default: int, minimum: int = 0) -> int:
    raw = str(os.environ.get(name, str(default))).strip()
    try:
        val = int(raw)
    except Exception:
        val = default
    return max(minimum, val)


def _utc_iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat()


def classify_provider_error(exc: Exception) -> Tuple[str, bool]:
    """
    Returns (error_class, is_quota_or_rate_limit).
    """
    status_code = getattr(exc, "status_code", None)
    try:
        status_code_int = int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        status_code_int = None
    text = repr(exc).lower()

    if status_code_int in {429, 402}:
        return ("quota_or_rate_limit", True)
    if status_code_int == 401:
        return ("auth_error", False)
    if status_code_int == 403:
        return ("permission_error", False)
    if status_code_int == 404:
        return ("not_found", False)
    if status_code_int is not None and status_code_int >= 500:
        return ("provider_upstream_error", False)

    quota_markers = (
        "429",
        "rate limit",
        "ratelimit",
        "too many requests",
        "insufficient_quota",
        "quota",
        "credit",
        "credits",
        "neurons",
        "monthly included",
        "daily limit",
    )
    for marker in quota_markers:
        if marker in text:
            return ("quota_or_rate_limit", True)

    if "invalid api key" in text or "unauthorized" in text:
        return ("auth_error", False)
    if "model" in text and "not found" in text:
        return ("model_not_found", False)

    return ("provider_api_error", False)


@dataclass(frozen=True)
class ProviderSelection:
    name: str
    backend: str
    base_url: str
    api_key: str
    model: str


class FreeProvidersExhaustedError(RuntimeError):
    def __init__(
        self,
        exhausted_providers: Sequence[str],
        unavailable_reasons: Optional[Dict[str, str]] = None,
        last_error_class: str = "all_free_providers_exhausted",
        provider_events: Optional[List[Dict[str, str]]] = None,
    ):
        self.exhausted_providers = list(exhausted_providers)
        self.unavailable_reasons = dict(unavailable_reasons or {})
        self.last_error_class = str(last_error_class or "all_free_providers_exhausted")
        self.provider_events = list(provider_events or [])
        reason = ", ".join(self.exhausted_providers) if self.exhausted_providers else "none"
        super().__init__(f"FREE_PROVIDERS_EXHAUSTED: exhausted={reason}")


class FreeProviderRouter:
    """
    Runtime router for free API providers + model pools.
    """

    def __init__(self):
        self._enabled = _env_bool("DRAGO_FREE_ONLY", default=False)
        order_raw = str(os.environ.get("DRAGO_FREE_PROVIDER_ORDER", "groq,openrouter,cloudflare,hf"))
        self._order = [x.strip().lower() for x in order_raw.split(",") if x.strip()]
        if not self._order:
            self._order = ["groq", "openrouter", "cloudflare", "hf"]
        self._mode = str(os.environ.get("DRAGO_MULTI_API_MODE", "adaptive_rr")).strip().lower() or "adaptive_rr"
        self._cooldown_sec = _parse_int("DRAGO_FREE_PROVIDER_COOLDOWN_SEC", default=600, minimum=1)
        self._lock = threading.Lock()

        self._cooldown_until: Dict[str, float] = {}
        self._failures: Dict[str, int] = {}
        self._stats: Dict[str, Dict[str, Any]] = {}

        self._model_cooldown_until: Dict[str, float] = {}
        self._model_failures: Dict[str, int] = {}
        self._model_stats: Dict[str, Dict[str, Any]] = {}

        self._last_selected: str = ""

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def provider_order(self) -> List[str]:
        return list(self._order)

    @property
    def last_selected(self) -> str:
        with self._lock:
            return self._last_selected

    def _model_key(self, provider: str, model: str) -> str:
        return f"{provider}::{model}"

    def _ensure_stat_locked(self, provider: str) -> Dict[str, Any]:
        stat = self._stats.get(provider)
        if not isinstance(stat, dict):
            stat = {
                "success_count": 0,
                "fail_count": 0,
                "last_error_class": "",
                "last_selected_at": 0.0,
                "last_success_at": 0.0,
                "last_failure_at": 0.0,
                "cooldown_until": 0.0,
            }
            self._stats[provider] = stat
        return stat

    def _ensure_model_stat_locked(self, provider: str, model: str) -> Dict[str, Any]:
        key = self._model_key(provider, model)
        stat = self._model_stats.get(key)
        if not isinstance(stat, dict):
            stat = {
                "success_count": 0,
                "fail_count": 0,
                "last_error_class": "",
                "last_selected_at": 0.0,
                "last_success_at": 0.0,
                "last_failure_at": 0.0,
                "cooldown_until": 0.0,
            }
            self._model_stats[key] = stat
        return stat

    def _parse_model_pool(self, raw: str, default: str) -> List[str]:
        txt = str(raw or "").strip() or default
        models = [x.strip() for x in txt.split(",") if x.strip()]
        dedup: List[str] = []
        seen: Set[str] = set()
        for m in models:
            if m in seen:
                continue
            seen.add(m)
            dedup.append(m)
        return dedup

    def _provider_model_pool(self, provider: str) -> List[str]:
        name = str(provider or "").strip().lower()
        if name == "groq":
            raw = os.environ.get("DRAGO_GROQ_MODEL_POOL") or os.environ.get("DRAGO_GROQ_MODEL") or ""
            return self._parse_model_pool(str(raw), "llama-3.1-8b-instant")
        if name == "openrouter":
            raw = os.environ.get("DRAGO_OPENROUTER_FREE_MODEL_POOL") or os.environ.get("DRAGO_OPENROUTER_FREE_MODEL") or ""
            return self._parse_model_pool(str(raw), "meta-llama/llama-3.3-70b-instruct:free")
        if name == "cloudflare":
            raw = os.environ.get("DRAGO_CLOUDFLARE_MODEL_POOL") or os.environ.get("DRAGO_CLOUDFLARE_MODEL") or ""
            return self._parse_model_pool(str(raw), "@cf/meta/llama-3.1-8b-instruct")
        if name == "hf":
            raw = os.environ.get("DRAGO_HF_MODEL_POOL") or os.environ.get("DRAGO_HF_MODEL") or ""
            return self._parse_model_pool(str(raw), "openai/gpt-oss-20b")
        return []

    def _build_provider_config(self, name: str) -> Optional[Dict[str, str]]:
        name = name.strip().lower()

        if name == "groq":
            api_key = str(os.environ.get("DRAGO_GROQ_API_KEY") or os.environ.get("GROQ_API_KEY") or "").strip()
            if not api_key:
                return None
            return {
                "backend": "openai",
                "base_url": "https://api.groq.com/openai/v1",
                "api_key": api_key,
            }

        if name == "openrouter":
            api_key = str(os.environ.get("DRAGO_OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_API_KEY") or "").strip()
            if not api_key:
                return None
            return {
                "backend": "openrouter",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": api_key,
            }

        if name == "cloudflare":
            token = str(os.environ.get("DRAGO_CLOUDFLARE_API_TOKEN") or "").strip()
            account_id = str(os.environ.get("DRAGO_CLOUDFLARE_ACCOUNT_ID") or "").strip()
            if not token or not account_id:
                return None
            return {
                "backend": "openai",
                "base_url": f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
                "api_key": token,
            }

        if name == "hf":
            token = str(os.environ.get("DRAGO_HF_TOKEN") or os.environ.get("HUGGINGFACEHUB_API_TOKEN") or "").strip()
            if not token:
                return None
            return {
                "backend": "openai",
                "base_url": "https://router.huggingface.co/v1",
                "api_key": token,
            }

        return None

    def state_snapshot(self) -> Dict[str, Dict[str, str]]:
        with self._lock:
            cooldown_iso = {name: _utc_iso(ts) for name, ts in self._cooldown_until.items()}
            failures = {k: int(v) for k, v in self._failures.items()}
            model_cooldown_iso = {name: _utc_iso(ts) for name, ts in self._model_cooldown_until.items()}
            model_failures = {k: int(v) for k, v in self._model_failures.items()}

            stats: Dict[str, Dict[str, Any]] = {}
            for provider, raw in self._stats.items():
                stats[provider] = {
                    "success_count": int(raw.get("success_count") or 0),
                    "fail_count": int(raw.get("fail_count") or 0),
                    "last_error_class": str(raw.get("last_error_class") or ""),
                    "last_selected_at": _utc_iso(float(raw.get("last_selected_at") or 0.0)) if raw.get("last_selected_at") else "",
                    "last_success_at": _utc_iso(float(raw.get("last_success_at") or 0.0)) if raw.get("last_success_at") else "",
                    "last_failure_at": _utc_iso(float(raw.get("last_failure_at") or 0.0)) if raw.get("last_failure_at") else "",
                    "cooldown_until": _utc_iso(float(raw.get("cooldown_until") or 0.0)) if raw.get("cooldown_until") else "",
                }

            model_stats: Dict[str, Dict[str, Any]] = {}
            for model_key, raw in self._model_stats.items():
                model_stats[model_key] = {
                    "success_count": int(raw.get("success_count") or 0),
                    "fail_count": int(raw.get("fail_count") or 0),
                    "last_error_class": str(raw.get("last_error_class") or ""),
                    "last_selected_at": _utc_iso(float(raw.get("last_selected_at") or 0.0)) if raw.get("last_selected_at") else "",
                    "last_success_at": _utc_iso(float(raw.get("last_success_at") or 0.0)) if raw.get("last_success_at") else "",
                    "last_failure_at": _utc_iso(float(raw.get("last_failure_at") or 0.0)) if raw.get("last_failure_at") else "",
                    "cooldown_until": _utc_iso(float(raw.get("cooldown_until") or 0.0)) if raw.get("cooldown_until") else "",
                }

        return {
            "failures": failures,
            "cooldown_until": cooldown_iso,
            "stats": stats,
            "model_failures": model_failures,
            "model_cooldown_until": model_cooldown_iso,
            "model_stats": model_stats,
            "mode": {"name": self._mode},
        }

    def mark_success(self, provider: str, model: Optional[str] = None) -> None:
        now = time.time()
        with self._lock:
            stat = self._ensure_stat_locked(provider)
            stat["success_count"] = int(stat.get("success_count") or 0) + 1
            stat["last_success_at"] = now
            stat["last_error_class"] = ""
            if model:
                model_stat = self._ensure_model_stat_locked(provider, model)
                model_stat["success_count"] = int(model_stat.get("success_count") or 0) + 1
                model_stat["last_success_at"] = now
                model_stat["last_error_class"] = ""

    def mark_failure(
        self,
        provider: str,
        error_class: str,
        *,
        model: Optional[str] = None,
        cooldown_until: float = 0.0,
    ) -> Dict[str, str]:
        now = time.time()
        with self._lock:
            self._failures[provider] = int(self._failures.get(provider) or 0) + 1
            stat = self._ensure_stat_locked(provider)
            stat["fail_count"] = int(stat.get("fail_count") or 0) + 1
            stat["last_error_class"] = str(error_class or "provider_api_error")
            stat["last_failure_at"] = now
            if cooldown_until > 0:
                stat["cooldown_until"] = cooldown_until
                self._cooldown_until[provider] = cooldown_until

            if model:
                model_key = self._model_key(provider, model)
                self._model_failures[model_key] = int(self._model_failures.get(model_key) or 0) + 1
                model_stat = self._ensure_model_stat_locked(provider, model)
                model_stat["fail_count"] = int(model_stat.get("fail_count") or 0) + 1
                model_stat["last_error_class"] = str(error_class or "provider_api_error")
                model_stat["last_failure_at"] = now
                if cooldown_until > 0:
                    model_stat["cooldown_until"] = cooldown_until
                    self._model_cooldown_until[model_key] = cooldown_until

        return {
            "type": "free_provider_exhausted",
            "provider": provider,
            "model": str(model or ""),
            "error_class": str(error_class or "provider_api_error"),
            "cooldown_until": _utc_iso(cooldown_until) if cooldown_until > 0 else "",
        }

    def mark_exhausted(self, provider: str, error_class: str, model: Optional[str] = None) -> Dict[str, str]:
        now = time.time()
        until = now + self._cooldown_sec
        return self.mark_failure(
            provider,
            error_class or "quota_or_rate_limit",
            model=model,
            cooldown_until=until,
        )

    def select(
        self,
        exclude: Optional[Sequence[str]] = None,
        exclude_models: Optional[Dict[str, Sequence[str]]] = None,
    ) -> Tuple[ProviderSelection, Optional[Dict[str, str]]]:
        excluded = {x.strip().lower() for x in (exclude or []) if str(x).strip()}
        excluded_models_map: Dict[str, Set[str]] = {}
        for provider_name, raw_models in (exclude_models or {}).items():
            p = str(provider_name or "").strip().lower()
            if not p:
                continue
            excluded_models_map[p] = {str(x).strip() for x in (raw_models or []) if str(x).strip()}

        now = time.time()
        unavailable: Dict[str, str] = {}
        candidates: List[str] = []
        provider_available_models: Dict[str, List[str]] = {}
        provider_configs: Dict[str, Dict[str, str]] = {}

        for name in self._order:
            if name in excluded:
                unavailable[name] = "already_exhausted_in_this_cycle"
                continue

            config = self._build_provider_config(name)
            if config is None:
                unavailable[name] = "missing_credentials_or_invalid_config"
                continue
            provider_configs[name] = config

            provider_cd = 0.0
            with self._lock:
                provider_cd = float(self._cooldown_until.get(name) or 0.0)
            if provider_cd > now:
                unavailable[name] = f"cooldown_until={_utc_iso(provider_cd)}"
                continue

            model_pool = self._provider_model_pool(name)
            if not model_pool:
                unavailable[name] = "empty_model_pool"
                continue

            available_models: List[str] = []
            blocked_models: List[str] = []
            excluded_models = excluded_models_map.get(name, set())
            for model_name in model_pool:
                if model_name in excluded_models:
                    blocked_models.append(f"{model_name}:already_exhausted_in_this_cycle")
                    continue

                mk = self._model_key(name, model_name)
                model_cd = 0.0
                with self._lock:
                    model_cd = float(self._model_cooldown_until.get(mk) or 0.0)
                if model_cd > now:
                    blocked_models.append(f"{model_name}:cooldown_until={_utc_iso(model_cd)}")
                    continue
                available_models.append(model_name)

            if not available_models:
                unavailable[name] = ";".join(blocked_models[:4]) if blocked_models else "all_models_unavailable"
                continue

            candidates.append(name)
            provider_available_models[name] = available_models

        if candidates:
            selected_name = candidates[0]
            if self._mode == "adaptive_rr":
                with self._lock:
                    def _provider_score(provider_name: str) -> Tuple[int, float, int]:
                        stat = self._ensure_stat_locked(provider_name)
                        fail_count = int(stat.get("fail_count") or 0)
                        success_count = int(stat.get("success_count") or 0)
                        last_selected_at = float(stat.get("last_selected_at") or 0.0)
                        order_idx = self._order.index(provider_name)
                        return (fail_count - success_count, last_selected_at, order_idx)

                    selected_name = sorted(candidates, key=_provider_score)[0]

            model_pool = self._provider_model_pool(selected_name)
            available_models = provider_available_models.get(selected_name) or [model_pool[0]]
            selected_model = available_models[0]

            if self._mode == "adaptive_rr":
                with self._lock:
                    def _model_score(model_name: str) -> Tuple[int, float, int]:
                        stat = self._ensure_model_stat_locked(selected_name, model_name)
                        fail_count = int(stat.get("fail_count") or 0)
                        success_count = int(stat.get("success_count") or 0)
                        last_selected_at = float(stat.get("last_selected_at") or 0.0)
                        order_idx = model_pool.index(model_name) if model_name in model_pool else 0
                        return (fail_count - success_count, last_selected_at, order_idx)

                    selected_model = sorted(available_models, key=_model_score)[0]

            config = provider_configs.get(selected_name) or {}
            selection = ProviderSelection(
                name=selected_name,
                backend=str(config.get("backend") or "openai"),
                base_url=str(config.get("base_url") or ""),
                api_key=str(config.get("api_key") or ""),
                model=selected_model,
            )

            with self._lock:
                previous = self._last_selected
                self._last_selected = selected_name
                provider_stat = self._ensure_stat_locked(selected_name)
                provider_stat["last_selected_at"] = now
                model_stat = self._ensure_model_stat_locked(selected_name, selected_model)
                model_stat["last_selected_at"] = now

            if not previous:
                event = {"type": "free_provider_selected", "provider": selected_name, "model": selected_model}
            elif previous != selected_name:
                event = {"type": "free_provider_switched", "from": previous, "to": selected_name, "model": selected_model, "mode": self._mode}
            else:
                event = {"type": "free_provider_selected", "provider": selected_name, "model": selected_model}

            return selection, event

        raise FreeProvidersExhaustedError(
            exhausted_providers=list(excluded),
            unavailable_reasons=unavailable,
            last_error_class="all_free_providers_exhausted",
        )
