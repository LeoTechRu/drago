"""
Drago — self-modifying AI agent.

Philosophy: BIBLE.md
Architecture: agent.py (orchestrator), tools/ (plugin tools),
              llm.py (LLM client), memory.py (memory), review.py (deep review),
              utils.py (shared utilities).
"""

# IMPORTANT: Do NOT import agent/loop/llm/etc here!
# colab_launcher.py imports drago.apply_patch, which triggers __init__.py.
# Any eager imports here get loaded into supervisor's memory and persist
# in forked worker processes as stale code, preventing hot-reload.
# Workers import make_agent directly from drago.agent.

__all__ = ['agent', 'tools', 'llm', 'memory', 'review', 'utils']

from pathlib import Path as _Path
import os as _os

# Backward compatibility with legacy Ouroboros environment variable names.
_ENV_ALIASES = {
    "DRAGO_MODEL": "OUROBOROS_MODEL",
    "DRAGO_MODEL_CODE": "OUROBOROS_MODEL_CODE",
    "DRAGO_MODEL_LIGHT": "OUROBOROS_MODEL_LIGHT",
    "DRAGO_MODEL_FALLBACK_LIST": "OUROBOROS_MODEL_FALLBACK_LIST",
    "DRAGO_WEBSEARCH_MODEL": "OUROBOROS_WEBSEARCH_MODEL",
    "DRAGO_MAX_WORKERS": "OUROBOROS_MAX_WORKERS",
    "DRAGO_MAX_ROUNDS": "OUROBOROS_MAX_ROUNDS",
    "DRAGO_BG_BUDGET_PCT": "OUROBOROS_BG_BUDGET_PCT",
    "DRAGO_SOFT_TIMEOUT_SEC": "OUROBOROS_SOFT_TIMEOUT_SEC",
    "DRAGO_HARD_TIMEOUT_SEC": "OUROBOROS_HARD_TIMEOUT_SEC",
    "DRAGO_DIAG_HEARTBEAT_SEC": "OUROBOROS_DIAG_HEARTBEAT_SEC",
    "DRAGO_DIAG_SLOW_CYCLE_SEC": "OUROBOROS_DIAG_SLOW_CYCLE_SEC",
    "DRAGO_WORKER_START_METHOD": "OUROBOROS_WORKER_START_METHOD",
    "DRAGO_BOOT_BRANCH": "OUROBOROS_BOOT_BRANCH",
    "DRAGO_REPO_DIR": "OUROBOROS_REPO_DIR",
    "DRAGO_CLAUDE_CODE_PERMISSION_MODE": "OUROBOROS_CLAUDE_CODE_PERMISSION_MODE",
    "DRAGO_PRE_PUSH_TESTS": "OUROBOROS_PRE_PUSH_TESTS",
}
for _new, _legacy in _ENV_ALIASES.items():
    if _new not in _os.environ and _legacy in _os.environ:
        _os.environ[_new] = _os.environ[_legacy]

__version__ = (_Path(__file__).resolve().parent.parent / 'VERSION').read_text(encoding='utf-8').strip()
