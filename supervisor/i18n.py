"""
Telegram-oriented localization helpers (default: Russian).
"""

from __future__ import annotations

from typing import Any, Callable, Dict


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


_RU: Dict[str, str] = {
    "panic_now": "🛑 PANIC: останавливаю все процессы.",
    "restarting_soft": "♻️ Перезапуск (soft).",
    "restart_cancelled": "⚠️ Перезапуск отменён: {reason}",
    "evolve_state": "🧬 Эволюция: {state}",
    "evolution_autostart_on": "🧬 Эволюция включена автоматически.",
    "bg_status": "🧠 Фоновое сознание: {status}",
    "owner_registered_online": "✅ Владелец зарегистрирован. Drago онлайн.",
    "photo_busy": "📎 Фото получено, но задача уже выполняется. Повтори отправку, когда освобожусь.",
    "restored_pending_tasks": "♻️ Восстановлена очередь из снапшота: {count} задач.",
    "task_stuck_restart": "⚠️ Задача зависла ({total}s без прогресса). Перезапускаю агент.",
    "task_running_progress": (
        "⏱️ Задача выполняется {total}s, "
        "последний прогресс был {idle}s назад. Продолжаю."
    ),
    "review_queued": "🔎 Ревью поставлено в очередь: {task_id} ({reason})",
    "evolution_paused_failures": (
        "🧬⚠️ Эволюция поставлена на паузу: {failures} подряд неуспешных циклов. "
        "Используй /evolve start после разбора причин."
    ),
    "evolution_stopped_budget": "💸 Эволюция остановлена: осталось ${remaining} (резерв ${reserve} для диалога).",
    "evolution_offline_started": "🧬 Оффлайн-эволюция #{cycle}: {task_id}",
    "evolution_started": "🧬 Эволюция #{cycle}: {task_id}",
    "evolution_sleeping_until": (
        "🧬😴 Лимиты free API исчерпаны. "
        "Следующая попытка эволюции после {wake_at} UTC "
        "(примерно через {wait_sec}с). Провайдеры в cooldown: {providers}."
    ),
    "task_soft_timeout": (
        "⏱️ Задача {task_id} выполняется {runtime}s. "
        "тип={task_type}, lag heartbeat={heartbeat_lag}s. Продолжаю."
    ),
    "task_hard_timeout_requeued": (
        "🛑 Hard-timeout: задача {task_id} остановлена после {runtime}s.\n"
        "Воркер {worker_id} перезапущен. Задача поставлена на retry attempt={attempt}."
    ),
    "task_hard_timeout_stopped": (
        "🛑 Hard-timeout: задача {task_id} остановлена после {runtime}s.\n"
        "Воркер {worker_id} перезапущен. Лимит retry исчерпан, задача завершена."
    ),
    "restart_requested_by_agent": "♻️ Перезапуск запрошен агентом: {reason}",
    "restart_skipped": "⚠️ Перезапуск пропущен: {reason}",
    "promoted_to_stable": "✅ Промоут: {from_branch} → {to_branch} ({sha})",
    "promote_failed": "❌ Не удалось промоутить stable: {error}",
    "task_rejected_depth": "⚠️ Задача отклонена: превышена глубина подзадач (3).",
    "task_rejected_duplicate": "⚠️ Задача отклонена: семантически дублирует активную задачу {task_id}.",
    "scheduled_task": "🗓️ Запланирована задача {task_id}: {desc}",
    "cancel_result": "{status} отмена {task_id} (event)",
    "evolution_via_tool": "🧬 Эволюция: {state} (через tool агента)",
    "consciousness_via_tool": "🧠 {result}",
    "bg_state_running": "включено",
    "bg_state_stopped": "выключено",
    "direct_chat_error": "⚠️ Ошибка: {error_class}: {error}",
    "worker_sha_mismatch": "⚠️ SHA воркера после запуска не совпал: ожидалось {expected}, получено {observed}",
    "crash_storm_direct_chat": (
        "⚠️ Частые падения воркеров. Multiprocessing отключён, "
        "перехожу в direct-chat режим (threading)."
    ),
    "evolution_task_started": "🧬 Эволюционная задача {task_id} запущена.",
    "review_task_started": "🔎 Задача ревью {task_id} запущена.",
    "evolution_report": (
        "🧬 Цикл #{cycle} | {status} | {reason}\n"
        "task={task_id} provider={provider} fallback={fallback} repo={repo_head}\n"
        "commit={commit_created} push={push_success} fail_streak={failures} next={next_step}"
    ),
    "bg_report": (
        "🧠 Фоновый отчёт\n"
        "- раундов: {rounds}\n"
        "- стоимость: ${cost}\n"
        "- заметка: {preview}"
    ),
    "bg_report_error": "🧠⚠️ Ошибка фонового цикла: {error}",
}


def t(key: str, **params: Any) -> str:
    template = _RU.get(key, key)
    try:
        return template.format_map(_SafeDict(params))
    except Exception:
        return template


def send_notice(send_with_budget_fn: Callable[..., Any], chat_id: int, key: str, **params: Any) -> str:
    text = t(key, **params)
    send_with_budget_fn(int(chat_id), text)
    return text
