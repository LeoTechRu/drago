"""
Supervisor — Task queue management.

Queue operations, priority, timeouts, persistence, evolution/review scheduling.
"""

from __future__ import annotations

import datetime
import json
import logging
import pathlib
import threading
import time
import uuid
import os
from typing import Any, Dict, List, Optional, Tuple

from supervisor.state import (
    load_state, save_state, append_jsonl, atomic_write_text,
    QUEUE_SNAPSHOT_PATH, budget_pct, TOTAL_BUDGET_LIMIT,
    budget_remaining, EVOLUTION_BUDGET_RESERVE,
)
from supervisor.telegram import send_with_budget
from supervisor.i18n import t

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level config (set via init())
# ---------------------------------------------------------------------------
DRIVE_ROOT: pathlib.Path = pathlib.Path("/content/drive/MyDrive/Drago")
SOFT_TIMEOUT_SEC: int = 600
HARD_TIMEOUT_SEC: int = 1800
HEARTBEAT_STALE_SEC: int = 120
QUEUE_MAX_RETRIES: int = 1


def init(drive_root: pathlib.Path, soft_timeout: int, hard_timeout: int) -> None:
    global DRIVE_ROOT, SOFT_TIMEOUT_SEC, HARD_TIMEOUT_SEC
    DRIVE_ROOT = drive_root
    SOFT_TIMEOUT_SEC = soft_timeout
    HARD_TIMEOUT_SEC = hard_timeout


# ---------------------------------------------------------------------------
# Queue data structures (references to workers module globals)
# ---------------------------------------------------------------------------
# These will be set by workers.init_queue_refs()
PENDING: List[Dict[str, Any]] = []
RUNNING: Dict[str, Dict[str, Any]] = {}
QUEUE_SEQ_COUNTER_REF: Dict[str, int] = {"value": 0}

# Lock for all mutations to PENDING, RUNNING, WORKERS shared collections.
# Protects against concurrent access from main loop, direct-chat threads, watchdog.
_queue_lock = threading.Lock()


def init_queue_refs(pending: List[Dict[str, Any]], running: Dict[str, Dict[str, Any]],
                    seq_counter_ref: Dict[str, int]) -> None:
    """Called by workers.py to provide references to queue data structures."""
    global PENDING, RUNNING, QUEUE_SEQ_COUNTER_REF
    PENDING = pending
    RUNNING = running
    QUEUE_SEQ_COUNTER_REF = seq_counter_ref


# ---------------------------------------------------------------------------
# Queue priority
# ---------------------------------------------------------------------------

def _task_priority(task_type: str) -> int:
    t = str(task_type or "").strip().lower()
    if t in ("task", "review"):
        return 0
    if t in ("evolution", "evolution_local"):
        return 1
    return 2


def _is_offline_evolution() -> bool:
    """Whether evolution should run without paid API calls."""
    return str(os.environ.get("DRAGO_OFFLINE_EVOLUTION", "0")).strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _evolution_failure_limit() -> int:
    """How many consecutive failed evolution cycles are allowed before auto-pause."""
    raw = str(os.environ.get("DRAGO_EVOLUTION_FAILURE_LIMIT", "1")).strip()
    try:
        value = int(raw)
    except Exception:
        value = 1
    return max(1, value)


def _evolution_notify_start() -> bool:
    """Send per-cycle start notifications to Telegram."""
    return _env_bool("DRAGO_EVOLUTION_NOTIFY_START", default=False)


def _free_lazy_mode_enabled() -> bool:
    return _env_bool("DRAGO_FREE_LAZY_MODE", default=True)


def _free_lazy_buffer_sec() -> int:
    raw = str(os.environ.get("DRAGO_FREE_LAZY_BUFFER_SEC", "30")).strip()
    try:
        val = int(raw)
    except Exception:
        val = 30
    return max(0, min(val, 3600))


def _free_lazy_notice_interval_sec() -> int:
    raw = str(os.environ.get("DRAGO_FREE_LAZY_NOTICE_INTERVAL_SEC", "300")).strip()
    try:
        val = int(raw)
    except Exception:
        val = 300
    return max(30, min(val, 3600))


def _free_provider_order() -> List[str]:
    raw = str(os.environ.get("DRAGO_FREE_PROVIDER_ORDER", "groq,openrouter,cloudflare,hf"))
    order = [x.strip().lower() for x in raw.split(",") if x.strip()]
    return order or ["groq", "openrouter", "cloudflare", "hf"]


def _provider_has_credentials(provider: str) -> bool:
    name = str(provider or "").strip().lower()
    if name == "groq":
        return bool(str(os.environ.get("DRAGO_GROQ_API_KEY") or os.environ.get("GROQ_API_KEY") or "").strip())
    if name == "openrouter":
        return bool(str(os.environ.get("DRAGO_OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_API_KEY") or "").strip())
    if name == "cloudflare":
        token = str(os.environ.get("DRAGO_CLOUDFLARE_API_TOKEN") or "").strip()
        account_id = str(os.environ.get("DRAGO_CLOUDFLARE_ACCOUNT_ID") or "").strip()
        return bool(token and account_id)
    if name == "hf":
        return bool(str(os.environ.get("DRAGO_HF_TOKEN") or os.environ.get("HUGGINGFACEHUB_API_TOKEN") or "").strip())
    return False


def _free_lazy_sleep_plan(st: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    If all credentialed free providers are in cooldown, return a wake-up plan.
    """
    if not _env_bool("DRAGO_FREE_ONLY", default=False):
        return None
    if not _free_lazy_mode_enabled():
        return None

    cooldown_map = dict(st.get("free_provider_cooldown_until") or {})
    now = time.time()
    waiting: List[Tuple[str, float]] = []
    ready_exists = False

    for provider in _free_provider_order():
        if not _provider_has_credentials(provider):
            continue
        cooldown_raw = str(cooldown_map.get(provider) or "").strip()
        cooldown_ts = parse_iso_to_ts(cooldown_raw) if cooldown_raw else None
        if cooldown_ts is not None and cooldown_ts > now:
            waiting.append((provider, cooldown_ts))
        else:
            ready_exists = True
            break

    if ready_exists or not waiting:
        return None

    wake_ts = min(ts for _, ts in waiting) + _free_lazy_buffer_sec()
    wait_sec = max(1, int(wake_ts - now))
    wake_iso = datetime.datetime.fromtimestamp(wake_ts, datetime.timezone.utc).isoformat()
    waiting_sorted = sorted(waiting, key=lambda x: x[1])
    providers = ",".join([name for name, _ in waiting_sorted]) or "-"
    return {
        "wake_ts": wake_ts,
        "wake_iso": wake_iso,
        "wait_sec": wait_sec,
        "providers": providers,
    }


def _queue_sort_key(task: Dict[str, Any]) -> Tuple[int, int]:
    _pr = task.get("priority")
    pr = int(_pr) if _pr is not None else _task_priority(str(task.get("type") or ""))
    _seq = task.get("_queue_seq")
    seq = int(_seq) if _seq is not None else 0
    return pr, seq


def sort_pending() -> None:
    """Sort PENDING queue by priority."""
    PENDING.sort(key=_queue_sort_key)


# ---------------------------------------------------------------------------
# Queue operations
# ---------------------------------------------------------------------------

def enqueue_task(task: Dict[str, Any], front: bool = False) -> Dict[str, Any]:
    """Add task to PENDING queue."""
    t = dict(task)
    QUEUE_SEQ_COUNTER_REF["value"] += 1
    seq = QUEUE_SEQ_COUNTER_REF["value"]
    t.setdefault("priority", _task_priority(str(t.get("type") or "")))
    _att = t.get("_attempt")
    t.setdefault("_attempt", int(_att) if _att is not None else 1)
    t["_queue_seq"] = -seq if front else seq
    t["queued_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    PENDING.append(t)
    sort_pending()
    return t


def queue_has_task_type(task_type: str) -> bool:
    """Check if a task of given type exists in PENDING or RUNNING."""
    tt = str(task_type or "")
    if any(str(t.get("type") or "") == tt for t in PENDING):
        return True
    for meta in RUNNING.values():
        task = meta.get("task") if isinstance(meta, dict) else None
        if isinstance(task, dict) and str(task.get("type") or "") == tt:
            return True
    return False


def persist_queue_snapshot(reason: str = "") -> None:
    """Save PENDING and RUNNING to snapshot file."""
    pending_rows = []
    for t in PENDING:
        pending_rows.append({
            "id": t.get("id"), "type": t.get("type"), "priority": t.get("priority"),
            "attempt": t.get("_attempt"), "queued_at": t.get("queued_at"),
            "queue_seq": t.get("_queue_seq"),
            "task": {
                "id": t.get("id"), "type": t.get("type"), "chat_id": t.get("chat_id"),
                "text": t.get("text"), "priority": t.get("priority"),
                "_attempt": t.get("_attempt"), "review_reason": t.get("review_reason"),
                "review_source_task_id": t.get("review_source_task_id"),
            },
        })
    running_rows = []
    now = time.time()
    for task_id, meta in RUNNING.items():
        task = meta.get("task") if isinstance(meta, dict) else {}
        started = float(meta.get("started_at") or 0.0) if isinstance(meta, dict) else 0.0
        hb = float(meta.get("last_heartbeat_at") or 0.0) if isinstance(meta, dict) else 0.0
        running_rows.append({
            "id": task_id, "type": task.get("type"), "priority": task.get("priority"),
            "attempt": meta.get("attempt"), "worker_id": meta.get("worker_id"),
            "runtime_sec": round(max(0.0, now - started), 2) if started > 0 else 0.0,
            "heartbeat_lag_sec": round(max(0.0, now - hb), 2) if hb > 0 else None,
            "soft_sent": bool(meta.get("soft_sent")), "task": task,
        })
    payload = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "reason": reason,
        "pending_count": len(PENDING), "running_count": len(RUNNING),
        "pending": pending_rows, "running": running_rows,
    }
    try:
        atomic_write_text(QUEUE_SNAPSHOT_PATH, json.dumps(payload, ensure_ascii=False, indent=2))
    except Exception:
        log.warning("Failed to persist queue snapshot (reason=%s)", reason, exc_info=True)
        pass


def parse_iso_to_ts(iso_ts: str) -> Optional[float]:
    """Parse ISO timestamp to Unix timestamp."""
    txt = str(iso_ts or "").strip()
    if not txt:
        return None
    try:
        return datetime.datetime.fromisoformat(txt.replace("Z", "+00:00")).timestamp()
    except Exception:
        log.debug("Failed to parse ISO timestamp: %s", txt, exc_info=True)
        return None


def restore_pending_from_snapshot(max_age_sec: int = 900) -> int:
    """Restore PENDING queue from snapshot file."""
    if PENDING:
        return 0
    try:
        if not QUEUE_SNAPSHOT_PATH.exists():
            return 0
        snap = json.loads(QUEUE_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        if not isinstance(snap, dict):
            return 0
        ts = str(snap.get("ts") or "")
        ts_unix = parse_iso_to_ts(ts)
        if ts_unix is None:
            return 0
        if (time.time() - ts_unix) > max_age_sec:
            return 0
        restored = 0
        for row in (snap.get("pending") or []):
            task = row.get("task") if isinstance(row, dict) else None
            if not isinstance(task, dict):
                continue
            if not task.get("id") or not task.get("chat_id"):
                continue
            enqueue_task(task)
            restored += 1
        if restored > 0:
            append_jsonl(
                DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "type": "queue_restored_from_snapshot",
                    "restored_pending": restored,
                },
            )
            persist_queue_snapshot(reason="queue_restored")
        return restored
    except Exception:
        log.warning("Failed to restore pending queue from snapshot", exc_info=True)
        return 0


def cancel_task_by_id(task_id: str) -> bool:
    """Cancel a task by ID (from PENDING or RUNNING)."""
    # Import here to avoid circular dependency during module load
    from supervisor import workers

    with _queue_lock:
        for i, t in enumerate(list(PENDING)):
            if t["id"] == task_id:
                PENDING.pop(i)
                persist_queue_snapshot(reason="cancel_pending")
                return True

        # For RUNNING tasks, need to terminate worker
        for w in workers.WORKERS.values():
            if w.busy_task_id == task_id:
                RUNNING.pop(task_id, None)
                if w.proc.is_alive():
                    w.proc.terminate()
                w.proc.join(timeout=5)
                workers.respawn_worker(w.wid)
                persist_queue_snapshot(reason="cancel_running")
                return True
    return False


# ---------------------------------------------------------------------------
# Timeout enforcement
# ---------------------------------------------------------------------------

def enforce_task_timeouts() -> None:
    """Check all RUNNING tasks for timeouts and enforce them."""
    # Import here to avoid circular dependency during module load
    from supervisor import workers
    
    if not RUNNING:
        return
    now = time.time()
    st = load_state()
    owner_chat_id = int(st.get("owner_chat_id") or 0)

    for task_id, meta in list(RUNNING.items()):
        if not isinstance(meta, dict):
            continue
        task = meta.get("task") if isinstance(meta.get("task"), dict) else {}
        started_at = float(meta.get("started_at") or 0.0)
        if started_at <= 0:
            continue
        last_hb = float(meta.get("last_heartbeat_at") or started_at)
        runtime_sec = max(0.0, now - started_at)
        hb_lag_sec = max(0.0, now - last_hb)
        hb_stale = hb_lag_sec >= HEARTBEAT_STALE_SEC
        _wid = meta.get("worker_id")
        worker_id = int(_wid) if _wid is not None else -1
        task_type = str(task.get("type") or "")
        _att = meta.get("attempt")
        if _att is None:
            _att = task.get("_attempt")
        attempt = int(_att) if _att is not None else 1

        if runtime_sec >= SOFT_TIMEOUT_SEC and not bool(meta.get("soft_sent")):
            meta["soft_sent"] = True
            if owner_chat_id:
                send_with_budget(
                    owner_chat_id,
                    t(
                        "task_soft_timeout",
                        task_id=task_id,
                        runtime=int(runtime_sec),
                        task_type=task_type,
                        heartbeat_lag=int(hb_lag_sec),
                    ),
                )

        if runtime_sec < HARD_TIMEOUT_SEC:
            continue

        RUNNING.pop(task_id, None)
        if worker_id in workers.WORKERS and workers.WORKERS[worker_id].busy_task_id == task_id:
            workers.WORKERS[worker_id].busy_task_id = None

        if worker_id in workers.WORKERS:
            w = workers.WORKERS[worker_id]
            try:
                if w.proc.is_alive():
                    w.proc.terminate()
                w.proc.join(timeout=5)
            except Exception:
                log.warning("Failed to terminate worker %d during hard timeout", worker_id, exc_info=True)
                pass
            workers.respawn_worker(worker_id)

        requeued = False
        new_attempt = attempt
        if attempt <= QUEUE_MAX_RETRIES and isinstance(task, dict):
            retried = dict(task)
            retried["original_task_id"] = task_id
            retried["id"] = uuid.uuid4().hex[:8]
            retried["_attempt"] = attempt + 1
            retried["timeout_retry_from"] = task_id
            retried["timeout_retry_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            enqueue_task(retried, front=True)
            requeued = True
            new_attempt = attempt + 1

        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "task_hard_timeout",
                "task_id": task_id, "task_type": task_type,
                "worker_id": worker_id, "runtime_sec": round(runtime_sec, 2),
                "heartbeat_lag_sec": round(hb_lag_sec, 2), "heartbeat_stale": hb_stale,
                "attempt": attempt, "requeued": requeued, "new_attempt": new_attempt,
                "max_retries": QUEUE_MAX_RETRIES,
            },
        )

        if owner_chat_id:
            if requeued:
                send_with_budget(
                    owner_chat_id,
                    t(
                        "task_hard_timeout_requeued",
                        task_id=task_id,
                        runtime=int(runtime_sec),
                        worker_id=worker_id,
                        attempt=new_attempt,
                    ),
                )
            else:
                send_with_budget(
                    owner_chat_id,
                    t(
                        "task_hard_timeout_stopped",
                        task_id=task_id,
                        runtime=int(runtime_sec),
                        worker_id=worker_id,
                    ),
                )

        persist_queue_snapshot(reason="task_hard_timeout")


# ---------------------------------------------------------------------------
# Evolution + review scheduling
# ---------------------------------------------------------------------------

def build_evolution_task_text(cycle: int) -> str:
    """
    Build evolution task text.

    Explicitly requires material repository progress so cycles do not degrade
    into chat-only/no-op responses.
    """
    return (
        f"EVOLUTION #{cycle}\n"
        "Objective: perform one minimal, safe, useful improvement in this repository.\n"
        "Hard requirements:\n"
        "1) Make a real code/config/docs change in-repo (no empty cycle).\n"
        "2) Keep scope minimal and directly justified.\n"
        "3) Use tools needed for implementation; avoid unrelated edits.\n"
        "4) Finish with repository progress markers (commit/push path if available).\n"
        "If blocked, produce the smallest unblocker change instead of a chat-only answer."
    )


def build_review_task_text(reason: str) -> str:
    """Build review task text. Minimal trigger — LLM decides scope and depth."""
    return f"REVIEW: {reason or 'owner request'}"


def queue_review_task(reason: str, force: bool = False) -> Optional[str]:
    """Queue a review task."""
    st = load_state()
    owner_chat_id = st.get("owner_chat_id")
    if not owner_chat_id:
        return None
    if (not force) and queue_has_task_type("review"):
        return None
    tid = uuid.uuid4().hex[:8]
    enqueue_task({
        "id": tid, "type": "review",
        "chat_id": int(owner_chat_id),
        "text": build_review_task_text(reason=reason),
    })
    persist_queue_snapshot(reason="review_enqueued")
    send_with_budget(int(owner_chat_id), t("review_queued", task_id=tid, reason=reason))
    return tid


def enqueue_evolution_task_if_needed() -> None:
    """Enqueue evolution task if queue is empty and evolution mode is enabled.

    Circuit breaker: pauses evolution after N consecutive failures
    (N from DRAGO_EVOLUTION_FAILURE_LIMIT, default=1) to prevent
    burning budget on no-op loops.
    """
    if PENDING or RUNNING:
        return
    st = load_state()
    if not bool(st.get("evolution_mode_enabled")):
        return
    owner_chat_id = st.get("owner_chat_id")
    if not owner_chat_id:
        return

    sleep_plan = _free_lazy_sleep_plan(st)
    if sleep_plan is not None:
        wake_iso = str(sleep_plan.get("wake_iso") or "")
        previous_wake = str(st.get("evolution_sleep_until") or "")
        st["evolution_sleep_until"] = wake_iso

        last_notice_raw = str(st.get("last_evolution_sleep_notice_at") or "")
        last_notice_ts = parse_iso_to_ts(last_notice_raw) or 0.0
        should_notify = (wake_iso != previous_wake) or ((time.time() - last_notice_ts) >= _free_lazy_notice_interval_sec())
        if should_notify:
            send_with_budget(
                int(owner_chat_id),
                t(
                    "evolution_sleeping_until",
                    wake_at=wake_iso,
                    wait_sec=int(sleep_plan.get("wait_sec") or 0),
                    providers=str(sleep_plan.get("providers") or "-"),
                ),
            )
            st["last_evolution_sleep_notice_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        save_state(st)
        return

    if st.get("evolution_sleep_until"):
        st["evolution_sleep_until"] = ""
        st["last_evolution_sleep_notice_at"] = ""
        save_state(st)
        st = load_state()

    # Circuit breaker: check for consecutive evolution failures
    failure_limit = _evolution_failure_limit()
    consecutive_failures = int(st.get("evolution_consecutive_failures") or 0)
    if consecutive_failures >= failure_limit:
        st["evolution_mode_enabled"] = False
        save_state(st)
        send_with_budget(
            int(owner_chat_id),
            t("evolution_paused_failures", failures=consecutive_failures),
        )
        return

    remaining = budget_remaining(st)
    if remaining < EVOLUTION_BUDGET_RESERVE:
        st["evolution_mode_enabled"] = False
        save_state(st)
        send_with_budget(
            int(owner_chat_id),
            t(
                "evolution_stopped_budget",
                remaining=f"{remaining:.2f}",
                reserve=f"{EVOLUTION_BUDGET_RESERVE:.0f}",
            ),
        )
        return
    offline_mode = _is_offline_evolution()
    cycle = int(st.get("evolution_cycle") or 0) + 1
    tid = uuid.uuid4().hex[:8]
    task_type = "evolution_local" if offline_mode else "evolution"
    enqueue_task({
        "id": tid, "type": task_type, "local": offline_mode,
        "chat_id": int(owner_chat_id),
        "text": build_evolution_task_text(cycle),
    })
    st["evolution_cycle"] = cycle
    st["last_evolution_task_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    save_state(st)
    if _evolution_notify_start():
        if offline_mode:
            send_with_budget(int(owner_chat_id), t("evolution_offline_started", cycle=cycle, task_id=tid))
        else:
            send_with_budget(int(owner_chat_id), t("evolution_started", cycle=cycle, task_id=tid))
