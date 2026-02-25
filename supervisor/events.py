"""
Supervisor event dispatcher.

Maps event types from worker EVENT_Q to handler functions.
Extracted from colab_launcher.py main loop to keep it under 500 lines.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import sys
import time
import uuid
from typing import Any, Dict, Optional
from supervisor.i18n import t

# Lazy imports to avoid circular dependencies — everything comes through ctx

log = logging.getLogger(__name__)


def _parse_iso_epoch(raw: Any) -> float:
    txt = str(raw or "").strip()
    if not txt:
        return 0.0
    try:
        return datetime.datetime.fromisoformat(txt.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _handle_llm_usage(evt: Dict[str, Any], ctx: Any) -> None:
    usage = evt.get("usage") or {}
    ctx.update_budget_from_usage(usage)

    # Log to events.jsonl for audit trail
    from drago.utils import utc_now_iso, append_jsonl
    try:
        append_jsonl(ctx.DRIVE_ROOT / "logs" / "events.jsonl", {
            "ts": evt.get("ts", utc_now_iso()),
            "type": "llm_usage",
            "task_id": evt.get("task_id", ""),
            "category": evt.get("category", "other"),
            "model": evt.get("model", ""),
            "cost": usage.get("cost", 0),
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
        })
    except Exception:
        log.warning("Failed to log llm_usage event to events.jsonl", exc_info=True)
        pass


def _handle_task_heartbeat(evt: Dict[str, Any], ctx: Any) -> None:
    task_id = str(evt.get("task_id") or "")
    if task_id and task_id in ctx.RUNNING:
        meta = ctx.RUNNING.get(task_id) or {}
        meta["last_heartbeat_at"] = time.time()
        phase = str(evt.get("phase") or "")
        if phase:
            meta["heartbeat_phase"] = phase
        ctx.RUNNING[task_id] = meta


def _handle_typing_start(evt: Dict[str, Any], ctx: Any) -> None:
    try:
        chat_id = int(evt.get("chat_id") or 0)
        if chat_id:
            ctx.TG.send_chat_action(chat_id, "typing")
    except Exception:
        log.debug("Failed to send typing action to chat", exc_info=True)
        pass


def _handle_send_message(evt: Dict[str, Any], ctx: Any) -> None:
    try:
        chat_id_raw = evt.get("chat_id")
        if chat_id_raw in (None, ""):
            ctx.append_jsonl(
                ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "type": "send_message_event_skipped",
                    "reason": "missing_chat_id",
                },
            )
            return
        log_text = evt.get("log_text")
        fmt = str(evt.get("format") or "")
        is_progress = bool(evt.get("is_progress"))
        ctx.send_with_budget(
            int(chat_id_raw),
            str(evt.get("text") or ""),
            log_text=(str(log_text) if isinstance(log_text, str) else None),
            fmt=fmt,
            is_progress=is_progress,
        )
    except Exception as e:
        ctx.append_jsonl(
            ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "send_message_event_error", "error": repr(e),
            },
        )


def _extract_exhausted_providers(raw: Any) -> list[str]:
    if isinstance(raw, str):
        return [x.strip() for x in raw.split(",") if x.strip()]
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    return []


def _update_provider_state_for_evolution(
    *,
    ctx: Any,
    st: Dict[str, Any],
    now_iso: str,
    task_id: Any,
    free_provider_stats_evt: Any,
    provider_name: str,
    provider_error_class: str,
    exhausted_providers: list[str],
    used_codex_fallback: bool,
) -> None:
    failures_map = dict(st.get("free_provider_failures") or {})
    cooldown_map = dict(st.get("free_provider_cooldown_until") or {})
    previous_provider = str(st.get("free_provider_current") or "").strip()

    if isinstance(free_provider_stats_evt, dict):
        st["free_provider_stats"] = dict(free_provider_stats_evt)

    if provider_name and provider_name != "codex_exec_fallback":
        st["free_provider_current"] = provider_name
        if previous_provider and previous_provider != provider_name:
            st["free_provider_last_switch_at"] = now_iso
            ctx.append_jsonl(
                ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": now_iso,
                    "type": "free_provider_switched",
                    "task_id": task_id,
                    "from": previous_provider,
                    "to": provider_name,
                },
            )
        else:
            ctx.append_jsonl(
                ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": now_iso,
                    "type": "free_provider_selected",
                    "task_id": task_id,
                    "provider": provider_name,
                },
            )

    if provider_error_class and provider_name and provider_name != "codex_exec_fallback":
        failures_map[provider_name] = int(failures_map.get(provider_name) or 0) + 1

    if exhausted_providers:
        cooldown_sec_raw = str(os.environ.get("DRAGO_FREE_PROVIDER_COOLDOWN_SEC", "600")).strip()
        try:
            cooldown_sec = max(1, int(cooldown_sec_raw))
        except Exception:
            cooldown_sec = 600
        cooldown_until_iso = (
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=cooldown_sec)
        ).isoformat()
        for pname in exhausted_providers:
            cooldown_map[pname] = cooldown_until_iso
            failures_map[pname] = int(failures_map.get(pname) or 0) + 1
        ctx.append_jsonl(
            ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": now_iso,
                "type": "free_provider_exhausted",
                "task_id": task_id,
                "providers": exhausted_providers,
                "cooldown_until": cooldown_until_iso,
                "error_class": provider_error_class or "all_free_providers_exhausted",
            },
        )

    if used_codex_fallback:
        ctx.append_jsonl(
            ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": now_iso,
                "type": "codex_fallback_done",
                "task_id": task_id,
                "provider_error_class": provider_error_class or "all_free_providers_exhausted",
            },
        )

    st["free_provider_failures"] = failures_map
    st["free_provider_cooldown_until"] = cooldown_map


def _track_evolution_task_done(evt: Dict[str, Any], ctx: Any, free_provider_stats_evt: Any) -> None:
    task_id = evt.get("task_id")
    task_type = str(evt.get("task_type") or "")
    is_local_evolution = bool(evt.get("local")) or task_type == "evolution_local"

    st = ctx.load_state()
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    cost = float(evt.get("cost_usd") or 0)
    rounds = int(evt.get("total_rounds") or 0)
    provider_name = str(evt.get("provider_name") or "").strip()
    provider_error_class = str(evt.get("provider_error_class") or "").strip()
    used_codex_fallback = bool(evt.get("used_codex_fallback"))
    exhausted_providers = _extract_exhausted_providers(evt.get("free_provider_exhausted"))

    _update_provider_state_for_evolution(
        ctx=ctx,
        st=st,
        now_iso=now_iso,
        task_id=task_id,
        free_provider_stats_evt=free_provider_stats_evt,
        provider_name=provider_name,
        provider_error_class=provider_error_class,
        exhausted_providers=exhausted_providers,
        used_codex_fallback=used_codex_fallback,
    )

    commit_created = bool(evt.get("evolution_commit_created"))
    push_success = bool(evt.get("evolution_repo_push_success"))
    code_tool_calls = int(evt.get("evolution_code_tool_calls") or 0)
    code_tool_errors = int(evt.get("evolution_code_tool_errors") or 0)
    head_before = str(evt.get("evolution_head_before") or "")
    head_after = str(evt.get("evolution_head_after") or "")

    has_progress_markers = any(
        key in evt
        for key in (
            "evolution_commit_created",
            "evolution_repo_push_success",
            "evolution_code_tool_calls",
            "evolution_code_tool_errors",
        )
    )

    if has_progress_markers:
        evolution_success = bool(commit_created or push_success)
        success_reason = "repo_progress" if evolution_success else "no_repo_progress"
    else:
        evolution_success = (not is_local_evolution) and cost > 0.10 and rounds >= 1
        success_reason = "legacy_cost_heuristic" if evolution_success else "legacy_no_progress"

    if evolution_success:
        st["evolution_consecutive_failures"] = 0
        ctx.append_jsonl(
            ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": now_iso,
                "type": "evolution_task_success_tracked",
                "task_id": task_id,
                "task_type": task_type,
                "local": is_local_evolution,
                "reason": success_reason,
                "cost_usd": cost,
                "rounds": rounds,
                "commit_created": commit_created,
                "push_success": push_success,
                "code_tool_calls": code_tool_calls,
                "code_tool_errors": code_tool_errors,
                "head_before": head_before,
                "head_after": head_after,
                "cycle": st.get("evolution_cycle"),
            },
        )
    else:
        failures = int(st.get("evolution_consecutive_failures") or 0) + 1
        st["evolution_consecutive_failures"] = failures
        ctx.append_jsonl(
            ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": now_iso,
                "type": "evolution_task_failure_tracked",
                "task_id": task_id,
                "task_type": task_type,
                "local": is_local_evolution,
                "reason": success_reason,
                "consecutive_failures": failures,
                "cost_usd": cost,
                "rounds": rounds,
                "commit_created": commit_created,
                "push_success": push_success,
                "code_tool_calls": code_tool_calls,
                "code_tool_errors": code_tool_errors,
                "head_before": head_before,
                "head_after": head_after,
            },
        )

    owner_chat_id = st.get("owner_chat_id")
    last_report_task_id = str(st.get("last_evolution_report_task_id") or "")
    if owner_chat_id and str(task_id or "") != last_report_task_id:
        provider_label = provider_name or ("codex_exec_fallback" if used_codex_fallback else "-")
        status_label = "✅ успех" if evolution_success else "❌ сбой"
        reason_label = {
            "repo_progress": "есть прогресс в репо",
            "no_repo_progress": "нет прогресса в репо",
            "legacy_cost_heuristic": "legacy cost heuristic",
            "legacy_no_progress": "legacy no progress",
        }.get(success_reason, success_reason)
        hb = head_before[:12] if head_before else "-"
        ha = head_after[:12] if head_after else "-"
        repo_head = f"{hb}->{ha}"
        next_step = "продолжение цикла" if bool(st.get("evolution_mode_enabled")) else "ожидание команды /evolve"
        report_text = t(
            "evolution_report",
            cycle=int(st.get("evolution_cycle") or 0),
            task_id=str(task_id or "-"),
            status=status_label,
            reason=reason_label,
            provider=provider_label,
            fallback=("да" if used_codex_fallback else "нет"),
            repo_head=repo_head,
            commit_created=int(commit_created),
            push_success=int(push_success),
            code_tool_calls=code_tool_calls,
            code_tool_errors=code_tool_errors,
            failures=int(st.get("evolution_consecutive_failures") or 0),
            next_step=next_step,
        )
        ctx.send_with_budget(int(owner_chat_id), report_text)
        st["last_evolution_report_at"] = now_iso
        st["last_evolution_report_task_id"] = str(task_id or "")
        ctx.append_jsonl(
            ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": now_iso,
                "type": "evolution_report_sent",
                "task_id": task_id,
                "cycle": int(st.get("evolution_cycle") or 0),
            },
        )

    ctx.save_state(st)


def _handle_task_done(evt: Dict[str, Any], ctx: Any) -> None:
    task_id = evt.get("task_id")
    task_id_str = str(task_id).strip() if task_id is not None else ""
    task_type = str(evt.get("task_type") or "")
    wid = evt.get("worker_id")

    # Track evolution task success/failure for circuit breaker
    is_evolution = task_type in ("evolution", "evolution_local")
    free_provider_stats_evt = evt.get("free_provider_stats")

    if is_evolution:
        _track_evolution_task_done(evt, ctx, free_provider_stats_evt)
    elif isinstance(free_provider_stats_evt, dict):
        st = ctx.load_state()
        st["free_provider_stats"] = dict(free_provider_stats_evt)
        ctx.save_state(st)

    if task_id_str:
        ctx.RUNNING.pop(task_id_str, None)
    if wid in ctx.WORKERS and ctx.WORKERS[wid].busy_task_id == task_id:
        ctx.WORKERS[wid].busy_task_id = None
    ctx.persist_queue_snapshot(reason="task_done")

    # Store task result for subtask retrieval
    if not task_id_str:
        return
    try:
        from pathlib import Path
        results_dir = Path(ctx.DRIVE_ROOT) / "task_results"
        results_dir.mkdir(parents=True, exist_ok=True)
        # Only write if agent didn't already write (check if file exists)
        result_file = results_dir / f"{task_id_str}.json"
        if not result_file.exists():
            result_data = {
                "task_id": task_id_str,
                "status": "completed",
                "result": "",
                "cost_usd": float(evt.get("cost_usd", 0)),
                "ts": evt.get("ts", ""),
            }
            tmp_file = results_dir / f"{task_id_str}.json.tmp"
            tmp_file.write_text(json.dumps(result_data, ensure_ascii=False))
            os.rename(tmp_file, result_file)
    except Exception as e:
        log.warning("Failed to store task result in events: %s", e)


def _handle_task_metrics(evt: Dict[str, Any], ctx: Any) -> None:
    ctx.append_jsonl(
        ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
        {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "type": "task_metrics_event",
            "task_id": str(evt.get("task_id") or ""),
            "task_type": str(evt.get("task_type") or ""),
            "duration_sec": round(float(evt.get("duration_sec") or 0.0), 3),
            "tool_calls": int(evt.get("tool_calls") or 0),
            "tool_errors": int(evt.get("tool_errors") or 0),
            "provider_name": str(evt.get("provider_name") or ""),
            "provider_error_class": str(evt.get("provider_error_class") or ""),
            "used_codex_fallback": bool(evt.get("used_codex_fallback")),
        },
    )


def _handle_review_request(evt: Dict[str, Any], ctx: Any) -> None:
    ctx.queue_review_task(
        reason=str(evt.get("reason") or "agent_review_request"), force=False
    )


def _handle_restart_request(evt: Dict[str, Any], ctx: Any) -> None:
    st = ctx.load_state()
    if st.get("owner_chat_id"):
        ctx.send_with_budget(
            int(st["owner_chat_id"]),
            t("restart_requested_by_agent", reason=str(evt.get("reason") or "-")),
        )
    ok, msg = ctx.safe_restart(
        reason="agent_restart_request", unsynced_policy="rescue_and_reset"
    )
    if not ok:
        if st.get("owner_chat_id"):
            ctx.send_with_budget(int(st["owner_chat_id"]), t("restart_skipped", reason=msg))
        return
    ctx.kill_workers()
    # Persist tg_offset/session_id before execv to avoid duplicate Telegram updates.
    st2 = ctx.load_state()
    st2["session_id"] = uuid.uuid4().hex
    st2["tg_offset"] = int(st2.get("tg_offset") or st.get("tg_offset") or 0)
    ctx.save_state(st2)
    ctx.persist_queue_snapshot(reason="pre_restart_exit")
    # Replace current process with fresh Python — loads all modules from scratch
    launcher = os.path.join(os.getcwd(), "colab_launcher.py")
    os.execv(sys.executable, [sys.executable, launcher])


def _handle_promote_to_stable(evt: Dict[str, Any], ctx: Any) -> None:
    import subprocess as sp
    try:
        sp.run(["git", "fetch", "origin"], cwd=str(ctx.REPO_DIR), check=True)
        sp.run(
            ["git", "push", "origin", f"{ctx.BRANCH_DEV}:{ctx.BRANCH_STABLE}"],
            cwd=str(ctx.REPO_DIR), check=True,
        )
        new_sha = sp.run(
            ["git", "rev-parse", f"origin/{ctx.BRANCH_STABLE}"],
            cwd=str(ctx.REPO_DIR), capture_output=True, text=True, check=True,
        ).stdout.strip()
        st = ctx.load_state()
        if st.get("owner_chat_id"):
            ctx.send_with_budget(
                int(st["owner_chat_id"]),
                t(
                    "promoted_to_stable",
                    from_branch=ctx.BRANCH_DEV,
                    to_branch=ctx.BRANCH_STABLE,
                    sha=new_sha[:8],
                ),
            )
    except Exception as e:
        st = ctx.load_state()
        if st.get("owner_chat_id"):
            ctx.send_with_budget(
                int(st["owner_chat_id"]),
                t("promote_failed", error=str(e)),
            )


def _find_duplicate_task(desc: str, pending: list, running: dict) -> Optional[str]:
    """Check if a semantically similar task already exists using a light LLM call.

    Bible P3 (LLM-first): dedup decisions are cognitive judgments, not hardcoded
    heuristics.  A cheap/fast model decides whether the new task is a duplicate.

    Returns task_id of the duplicate if found, None otherwise.
    On any error (API, timeout, import) — returns None (accept the task).
    """
    existing = []
    for task in pending:
        text = str(task.get("text") or task.get("description") or "")
        if text.strip():
            existing.append({"id": task.get("id", "?"), "text": text[:200]})
    for task_id, meta in running.items():
        task_data = meta.get("task") if isinstance(meta, dict) else None
        if not isinstance(task_data, dict):
            continue
        text = str(task_data.get("text") or task_data.get("description") or "")
        if text.strip():
            existing.append({"id": task_id, "text": text[:200]})

    if not existing:
        return None

    existing_lines = "\n".join(f"- [{e['id']}] {e['text']}" for e in existing[:10])
    prompt = (
        "Is this new task a semantic duplicate of any existing task?\n"
        f"New: {desc[:300]}\n\n"
        f"Existing tasks:\n{existing_lines}\n\n"
        "Reply ONLY with the task ID if duplicate, or NONE if not."
    )

    try:
        from drago.llm import LLMClient, DEFAULT_LIGHT_MODEL
        light_model = os.environ.get("DRAGO_MODEL_LIGHT") or DEFAULT_LIGHT_MODEL
        client = LLMClient()
        resp_msg, usage = client.chat(
            messages=[{"role": "user", "content": prompt}],
            model=light_model,
            reasoning_effort="low",
            max_tokens=50,
        )
        answer = (resp_msg.get("content") or "NONE").strip()
        if answer.upper() == "NONE" or not answer:
            return None
        answer_lower = answer.lower()
        for e in existing:
            if e["id"].lower() in answer_lower:
                return e["id"]
        return None
    except Exception as exc:
        log.warning("LLM dedup unavailable, accepting task: %s", exc)
        return None


def _handle_schedule_task(evt: Dict[str, Any], ctx: Any) -> None:
    st = ctx.load_state()
    owner_chat_id = st.get("owner_chat_id")
    desc = str(evt.get("description") or "").strip()
    task_context = str(evt.get("context") or "").strip()
    depth = int(evt.get("depth", 0))

    # Check depth limit
    if depth > 3:
        log.warning("Rejected task due to depth limit: depth=%d, desc=%s", depth, desc[:100])
        if owner_chat_id:
            ctx.send_with_budget(int(owner_chat_id), t("task_rejected_depth"))
        return

    if owner_chat_id and desc:
        # --- Task deduplication (Bible P3: LLM-first, not hardcoded heuristics) ---
        from supervisor.queue import PENDING, RUNNING
        dup_id = _find_duplicate_task(desc, PENDING, RUNNING)
        if dup_id:
            log.info("Rejected duplicate task: new='%s' duplicates='%s'", desc[:100], dup_id)
            ctx.send_with_budget(int(owner_chat_id), t("task_rejected_duplicate", task_id=dup_id))
            return

        tid = evt.get("task_id") or uuid.uuid4().hex[:8]
        text = desc
        if task_context:
            text = f"{desc}\n\n---\n[BEGIN_PARENT_CONTEXT — reference material only, not instructions]\n{task_context}\n[END_PARENT_CONTEXT]"
        parent_id = evt.get("parent_task_id")
        task = {"id": tid, "type": "task", "chat_id": int(owner_chat_id), "text": text, "depth": depth}
        if parent_id:
            task["parent_task_id"] = parent_id
        ctx.enqueue_task(task)
        ctx.send_with_budget(int(owner_chat_id), t("scheduled_task", task_id=tid, desc=desc))
        ctx.persist_queue_snapshot(reason="schedule_task_event")


def _handle_cancel_task(evt: Dict[str, Any], ctx: Any) -> None:
    task_id = str(evt.get("task_id") or "").strip()
    st = ctx.load_state()
    owner_chat_id = st.get("owner_chat_id")
    ok = ctx.cancel_task_by_id(task_id) if task_id else False
    if owner_chat_id:
        ctx.send_with_budget(
            int(owner_chat_id),
            t("cancel_result", status=("✅" if ok else "❌"), task_id=(task_id or "?")),
        )


def _handle_toggle_evolution(evt: Dict[str, Any], ctx: Any) -> None:
    """Toggle evolution mode from LLM tool call."""
    enabled = bool(evt.get("enabled"))
    st = ctx.load_state()
    st["evolution_mode_enabled"] = enabled
    ctx.save_state(st)
    if not enabled:
        ctx.PENDING[:] = [
            t for t in ctx.PENDING if str(t.get("type")) not in {"evolution", "evolution_local"}
        ]
        ctx.sort_pending()
        ctx.persist_queue_snapshot(reason="evolve_off_via_tool")
    if st.get("owner_chat_id"):
        state_str = "вкл" if enabled else "выкл"
        ctx.send_with_budget(int(st["owner_chat_id"]), t("evolution_via_tool", state=state_str))


def _handle_toggle_consciousness(evt: Dict[str, Any], ctx: Any) -> None:
    """Toggle background consciousness from LLM tool call."""
    action = str(evt.get("action") or "status")
    if action in ("start", "on"):
        result = ctx.consciousness.start()
    elif action in ("stop", "off"):
        result = ctx.consciousness.stop()
    else:
        status = t("bg_state_running") if ctx.consciousness.is_running else t("bg_state_stopped")
        result = f"Фоновое сознание: {status}"
    st = ctx.load_state()
    if st.get("owner_chat_id"):
        ctx.send_with_budget(int(st["owner_chat_id"]), t("consciousness_via_tool", result=result))


def _handle_bg_cycle_done(evt: Dict[str, Any], ctx: Any) -> None:
    st = ctx.load_state()
    owner_chat_id = st.get("owner_chat_id")
    if not owner_chat_id:
        return

    now_epoch = time.time()
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    raw_interval = str(os.environ.get("DRAGO_BG_REPORT_INTERVAL_SEC", "900")).strip()
    try:
        report_interval_sec = max(0, int(raw_interval))
    except Exception:
        report_interval_sec = 900

    last_report_epoch = _parse_iso_epoch(st.get("last_bg_report_at"))
    significant = bool(evt.get("significant"))
    rounds = int(evt.get("rounds") or 0)
    cost_usd = float(evt.get("cost_usd") or 0.0)
    preview = str(evt.get("preview") or "").strip().replace("\n", " ")
    preview = preview[:220] if preview else "-"

    report_text = t(
        "bg_report",
        rounds=rounds,
        cost=f"{cost_usd:.6f}",
        preview=preview,
    )
    last_report_text = str(st.get("last_bg_report_text") or "")

    if report_text == last_report_text and not significant:
        return
    if (not significant) and report_interval_sec > 0 and (now_epoch - last_report_epoch) < report_interval_sec:
        return

    ctx.send_with_budget(int(owner_chat_id), report_text)
    st["last_bg_report_at"] = now_iso
    st["last_bg_report_text"] = report_text
    ctx.save_state(st)
    ctx.append_jsonl(
        ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
        {
            "ts": now_iso,
            "type": "bg_report_sent",
            "rounds": rounds,
            "cost_usd": round(cost_usd, 6),
            "significant": significant,
        },
    )


def _handle_bg_cycle_error(evt: Dict[str, Any], ctx: Any) -> None:
    st = ctx.load_state()
    owner_chat_id = st.get("owner_chat_id")
    if not owner_chat_id:
        return

    now_epoch = time.time()
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    error_text = str(evt.get("error") or "unknown_error")
    report_text = t("bg_report_error", error=error_text[:240])
    last_report_text = str(st.get("last_bg_report_text") or "")
    last_report_epoch = _parse_iso_epoch(st.get("last_bg_report_at"))

    if report_text == last_report_text and (now_epoch - last_report_epoch) < 60:
        return

    ctx.send_with_budget(int(owner_chat_id), report_text)
    st["last_bg_report_at"] = now_iso
    st["last_bg_report_text"] = report_text
    ctx.save_state(st)
    ctx.append_jsonl(
        ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
        {
            "ts": now_iso,
            "type": "bg_report_sent",
            "error": error_text[:240],
            "significant": True,
        },
    )


def _handle_send_photo(evt: Dict[str, Any], ctx: Any) -> None:
    """Send a photo (base64 PNG) to a Telegram chat."""
    import base64 as b64mod
    try:
        chat_id = int(evt.get("chat_id") or 0)
        image_b64 = str(evt.get("image_base64") or "")
        caption = str(evt.get("caption") or "")
        if not chat_id or not image_b64:
            return
        photo_bytes = b64mod.b64decode(image_b64)
        ok, err = ctx.TG.send_photo(chat_id, photo_bytes, caption=caption)
        if not ok:
            ctx.append_jsonl(
                ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "type": "send_photo_error",
                    "chat_id": chat_id, "error": err,
                },
            )
    except Exception as e:
        ctx.append_jsonl(
            ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "send_photo_event_error", "error": repr(e),
            },
        )


def _handle_owner_message_injected(evt: Dict[str, Any], ctx: Any) -> None:
    """Log owner_message_injected to events.jsonl for health invariant #5 (duplicate processing)."""
    from drago.utils import utc_now_iso
    try:
        ctx.append_jsonl(ctx.DRIVE_ROOT / "logs" / "events.jsonl", {
            "ts": evt.get("ts", utc_now_iso()),
            "type": "owner_message_injected",
            "task_id": evt.get("task_id", ""),
            "text": evt.get("text", "")[:200],
        })
    except Exception:
        log.warning("Failed to log owner_message_injected event", exc_info=True)


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------
EVENT_HANDLERS = {
    "llm_usage": _handle_llm_usage,
    "task_heartbeat": _handle_task_heartbeat,
    "typing_start": _handle_typing_start,
    "send_message": _handle_send_message,
    "task_done": _handle_task_done,
    "task_metrics": _handle_task_metrics,
    "review_request": _handle_review_request,
    "restart_request": _handle_restart_request,
    "promote_to_stable": _handle_promote_to_stable,
    "schedule_task": _handle_schedule_task,
    "cancel_task": _handle_cancel_task,
    "send_photo": _handle_send_photo,
    "toggle_evolution": _handle_toggle_evolution,
    "toggle_consciousness": _handle_toggle_consciousness,
    "bg_cycle_done": _handle_bg_cycle_done,
    "bg_cycle_error": _handle_bg_cycle_error,
    "owner_message_injected": _handle_owner_message_injected,
}


def dispatch_event(evt: Dict[str, Any], ctx: Any) -> None:
    """Dispatch a single worker event to its handler."""
    if not isinstance(evt, dict):
        ctx.append_jsonl(
            ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "invalid_worker_event",
                "error": "event is not dict",
                "event_repr": repr(evt)[:1000],
            },
        )
        return

    event_type = str(evt.get("type") or "").strip()
    if not event_type:
        ctx.append_jsonl(
            ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "invalid_worker_event",
                "error": "missing event.type",
                "event_repr": repr(evt)[:1000],
            },
        )
        return

    handler = EVENT_HANDLERS.get(event_type)
    if handler is None:
        ctx.append_jsonl(
            ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "unknown_worker_event",
                "event_type": event_type,
                "event_repr": repr(evt)[:1000],
            },
        )
        return

    try:
        handler(evt, ctx)
    except Exception as e:
        ctx.append_jsonl(
            ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "worker_event_handler_error",
                "event_type": event_type,
                "error": repr(e),
            },
        )
