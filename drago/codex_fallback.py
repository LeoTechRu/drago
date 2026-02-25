"""
Codex exec fallback runner when free providers are exhausted.
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import shutil
import subprocess
from typing import Any, Dict, List, Tuple


def _utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _append_jsonl(path: pathlib.Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _git_head(repo_dir: pathlib.Path) -> str:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return ""


def _parse_timeout_sec(task: Dict[str, Any], *, default: int = 900, minimum: int = 60) -> int:
    raw = task.get("codex_fallback_timeout_sec")
    if raw is None:
        return max(minimum, default)
    try:
        value = int(str(raw).strip())
    except Exception:
        return max(minimum, default)
    return max(minimum, value)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _build_fallback_prompt(task: Dict[str, Any]) -> str:
    task_type = str(task.get("type") or "task").strip().lower()
    task_text = str(task.get("text") or "").strip()
    if task_type in {"evolution", "evolution_local"}:
        return (
            "You are Codex fallback for Drago evolution.\n"
            "Do exactly ONE minimal, real repository improvement.\n"
            "Constraints:\n"
            "- No unrelated edits.\n"
            "- Keep changes small and deterministic.\n"
            "- Prefer reliability/safety/operability improvements.\n"
            "- End with a short plain-text report of what changed.\n\n"
            f"Current evolution task context:\n{task_text}\n"
        )
    return (
        "You are Codex fallback for Drago owner communication.\n"
        "Primary goal: produce a concise actionable response in Russian.\n"
        "Constraints:\n"
        "- No unrelated repository edits.\n"
        "- If code edit is truly required: keep it minimal and scoped.\n"
        "- Prefer explanation/plan over broad refactor.\n\n"
        f"Current task context:\n{task_text}\n"
    )


def run_codex_fallback(
    *,
    repo_dir: pathlib.Path,
    drive_root: pathlib.Path,
    task: Dict[str, Any],
    exhausted_providers: List[str],
    provider_error_class: str,
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    logs_dir = drive_root / "logs"
    supervisor_log = logs_dir / "supervisor.jsonl"
    fallback_log = logs_dir / "codex_fallback.log"
    task_id = str(task.get("id") or "")
    started = _utc_iso()

    _append_jsonl(
        supervisor_log,
        {
            "ts": started,
            "type": "codex_fallback_started",
            "task_id": task_id,
            "exhausted_providers": exhausted_providers,
            "provider_error_class": provider_error_class,
        },
    )

    head_before = _git_head(repo_dir)
    prompt = _build_fallback_prompt(task)
    cmd = [
        "codex",
        "exec",
        "--json",
        "--full-auto",
        "-C",
        str(repo_dir),
        prompt,
    ]

    codex_exists = shutil.which("codex") is not None
    return_code = 127
    stdout = ""
    stderr = ""
    timeout_sec = _parse_timeout_sec(task)
    timed_out = False

    if codex_exists:
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                check=False,
            )
            return_code = int(proc.returncode)
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
        except subprocess.TimeoutExpired as e:
            timed_out = True
            return_code = 124
            stdout = str(getattr(e, "stdout", "") or "")
            stderr = str(getattr(e, "stderr", "") or "")
        except Exception as e:
            return_code = 1
            stderr = repr(e)
    else:
        stderr = "codex binary not found in PATH"

    task_type = str(task.get("type") or "task").strip().lower()
    is_evolution = task_type in {"evolution", "evolution_local"}
    head_after = _git_head(repo_dir)
    repo_progress = bool(head_before and head_after and head_before != head_after)

    tail_stdout = "\n".join([line for line in stdout.splitlines()[-20:] if line.strip()])
    tail_stderr = "\n".join([line for line in stderr.splitlines()[-20:] if line.strip()])

    with fallback_log.open("a", encoding="utf-8") as f:
        f.write(f"[{started}] task_id={task_id}\n")
        f.write(f"cmd={' '.join(cmd[:-1])} <prompt>\n")
        f.write(f"return_code={return_code} timed_out={int(timed_out)}\n")
        f.write(f"head_before={head_before[:12]} head_after={head_after[:12]}\n")
        if tail_stdout:
            f.write("stdout_tail:\n")
            f.write(tail_stdout + "\n")
        if tail_stderr:
            f.write("stderr_tail:\n")
            f.write(tail_stderr + "\n")
        f.write("---\n")

    finished = _utc_iso()
    _append_jsonl(
        supervisor_log,
        {
            "ts": finished,
            "type": "codex_fallback_done",
            "task_id": task_id,
            "task_type": task_type,
            "return_code": return_code,
            "timed_out": timed_out,
            "repo_progress": repo_progress,
            "success": (repo_progress if is_evolution else bool(return_code == 0)),
            "head_before": head_before[:12],
            "head_after": head_after[:12],
        },
    )

    success = repo_progress if is_evolution else bool(return_code == 0)
    status = "success" if success else ("no_repo_progress" if is_evolution else "fallback_failed")
    exhausted_text = ", ".join(exhausted_providers) if exhausted_providers else "none"
    include_tails = _env_bool("DRAGO_CODEX_FALLBACK_INCLUDE_TAILS", default=False)
    summary = (
        f"🛟 Codex fallback: {status}\n"
        f"task={task_id} rc={return_code} repo_progress={int(repo_progress)}\n"
        f"provider_error={provider_error_class} exhausted={exhausted_text}\n"
        f"repo={head_before[:12] or '-'}->{head_after[:12] or '-'}"
    )
    if include_tails:
        if tail_stdout:
            summary += f"\nstdout_tail:\n{tail_stdout[:500]}\n"
        if tail_stderr:
            summary += f"\nstderr_tail:\n{tail_stderr[:500]}\n"

    usage: Dict[str, Any] = {
        "cost": 0.0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "rounds": 1 if success else 0,
        "provider_name": "codex_exec_fallback",
        "provider_error_class": provider_error_class or "all_free_providers_exhausted",
        "used_codex_fallback": True,
        "free_provider_exhausted": ",".join([x for x in exhausted_providers if str(x).strip()]),
        "evolution_commit_created": repo_progress if is_evolution else False,
        "evolution_repo_push_success": False,
        "evolution_code_tool_calls": 1 if is_evolution else 0,
        "evolution_code_tool_errors": 0 if (is_evolution and repo_progress) else (1 if is_evolution else 0),
        "evolution_head_before": head_before[:12],
        "evolution_head_after": head_after[:12],
    }

    llm_trace = {
        "assistant_notes": [summary.strip()],
        "tool_calls": [],
    }
    return summary.strip(), usage, llm_trace
