"""Minimal Colab boot shim.

Paste this file contents into the only immutable Colab cell.
The shim stays tiny and only starts the runtime launcher from repository.
"""

import os
import pathlib
import subprocess
import sys
from typing import Optional

try:
    from google.colab import userdata  # type: ignore
    from google.colab import drive  # type: ignore
    _HAS_COLAB_RUNTIME = True
except Exception:
    userdata = None
    drive = None
    _HAS_COLAB_RUNTIME = False


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _coerce_path(name: str, default: str) -> pathlib.Path:
    return pathlib.Path(str(os.environ.get(name, default))).resolve()


def _legacy_var_name(name: str) -> Optional[str]:
    if name.startswith("DRAGO_"):
        return f"OUROBOROS_{name.removeprefix('DRAGO_')}"
    if name.startswith("OUROBOROS_"):
        return f"DRAGO_{name.removeprefix('OUROBOROS_')}"
    return None


def _userdata_get(name: str) -> Optional[str]:
    if userdata is None:
        return None
    try:
        return userdata.get(name)
    except Exception:
        return None


def get_secret(name: str, required: bool = False) -> Optional[str]:
    v = _userdata_get(name)
    if v is None or str(v).strip() == "":
        v = os.environ.get(name)
    if v is None or str(v).strip() == "":
        legacy = _legacy_var_name(name)
        if legacy:
            v = _userdata_get(legacy)
            if v is None or str(v).strip() == "":
                v = os.environ.get(legacy)
    if required:
        assert v is not None and str(v).strip() != "", f"Missing required secret: {name}"
    return v


def export_secret_to_env(name: str, required: bool = False) -> Optional[str]:
    val = get_secret(name, required=required)
    if val is not None and str(val).strip() != "":
        os.environ[name] = str(val)
    return val


DRAGO_LOCAL_MODE = _env_bool("DRAGO_LOCAL_MODE", default=not _HAS_COLAB_RUNTIME)
DRAGO_SKIP_GIT_BOOTSTRAP = _env_bool("DRAGO_SKIP_GIT_BOOTSTRAP", default=DRAGO_LOCAL_MODE)
DRAGO_FAKE_TELEGRAM = _env_bool("DRAGO_FAKE_TELEGRAM", default=DRAGO_LOCAL_MODE)
DRAGO_OFFLINE_EVOLUTION = _env_bool("DRAGO_OFFLINE_EVOLUTION", default=False)

_legacy_drive_root = os.environ.get("OUROBOROS_DRIVE_ROOT")
if _legacy_drive_root and "DRAGO_DRIVE_ROOT" not in os.environ:
    os.environ["DRAGO_DRIVE_ROOT"] = _legacy_drive_root

_legacy_repo_dir = os.environ.get("OUROBOROS_REPO_DIR")
if _legacy_repo_dir and "DRAGO_REPO_DIR" not in os.environ:
    os.environ["DRAGO_REPO_DIR"] = _legacy_repo_dir

_legacy_boot_branch = os.environ.get("OUROBOROS_BOOT_BRANCH")
if _legacy_boot_branch and "DRAGO_BOOT_BRANCH" not in os.environ:
    os.environ["DRAGO_BOOT_BRANCH"] = _legacy_boot_branch

# Export required runtime secrets so subprocess launcher can always read env fallback.
for _name in ("OPENROUTER_API_KEY", "TOTAL_BUDGET", "GITHUB_TOKEN"):
    export_secret_to_env(_name, required=not DRAGO_SKIP_GIT_BOOTSTRAP)
if DRAGO_FAKE_TELEGRAM:
    val = get_secret("TELEGRAM_BOT_TOKEN", required=False)
    if val is None:
        os.environ["TELEGRAM_BOT_TOKEN"] = "fake-token"
    else:
        export_secret_to_env("TELEGRAM_BOT_TOKEN", required=False)
else:
    export_secret_to_env("TELEGRAM_BOT_TOKEN", required=True)

# Optional secrets (keep empty if missing).
for _name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
    export_secret_to_env(_name, required=False)

# Colab diagnostics defaults (override in config cell if needed).
os.environ.setdefault("DRAGO_WORKER_START_METHOD", os.environ.get("OUROBOROS_WORKER_START_METHOD", "fork"))
os.environ.setdefault("DRAGO_DIAG_HEARTBEAT_SEC", os.environ.get("OUROBOROS_DIAG_HEARTBEAT_SEC", "30"))
os.environ.setdefault("DRAGO_DIAG_SLOW_CYCLE_SEC", os.environ.get("OUROBOROS_DIAG_SLOW_CYCLE_SEC", "20"))
os.environ.setdefault("PYTHONUNBUFFERED", "1")

GITHUB_TOKEN = str(os.environ.get("GITHUB_TOKEN", ""))
GITHUB_USER = os.environ.get("GITHUB_USER", "").strip()
GITHUB_REPO = os.environ.get("GITHUB_REPO", "drago").strip()
assert DRAGO_SKIP_GIT_BOOTSTRAP or GITHUB_USER, "GITHUB_USER not set. Add it to your config cell (see README)."

BOOT_BRANCH = str(os.environ.get("DRAGO_BOOT_BRANCH", os.environ.get("OUROBOROS_BOOT_BRANCH", "drago")))

REPO_DIR = _coerce_path(
    "DRAGO_REPO_DIR",
    str(pathlib.Path(__file__).resolve().parent / "drago_repo"),
)
if not DRAGO_LOCAL_MODE and "DRAGO_REPO_DIR" not in os.environ:
    REPO_DIR = pathlib.Path("/content/drago_repo").resolve()

if not DRAGO_SKIP_GIT_BOOTSTRAP:
    remote = (
        f"https://{GITHUB_TOKEN}:x-oauth-basic@github.com/{GITHUB_USER}/{GITHUB_REPO}.git"
        if GITHUB_TOKEN.strip()
        else f"https://github.com/{GITHUB_USER}/{GITHUB_REPO}.git"
    )
    if not (REPO_DIR / ".git").exists():
        subprocess.run(["rm", "-rf", str(REPO_DIR)], check=False)
        subprocess.run(["git", "clone", remote, str(REPO_DIR)], check=True)
    else:
        subprocess.run(["git", "remote", "set-url", "origin", remote], cwd=str(REPO_DIR), check=True)

    subprocess.run(["git", "fetch", "origin"], cwd=str(REPO_DIR), check=True)

    # Check if BOOT_BRANCH exists on the fork's remote.
    # New forks (from the main-only public repo) won't have it yet.
    _rc = subprocess.run(
        ["git", "rev-parse", "--verify", f"origin/{BOOT_BRANCH}"],
        cwd=str(REPO_DIR), capture_output=True,
    ).returncode

    if _rc == 0:
        subprocess.run(["git", "checkout", BOOT_BRANCH], cwd=str(REPO_DIR), check=True)
        subprocess.run(["git", "reset", "--hard", f"origin/{BOOT_BRANCH}"], cwd=str(REPO_DIR), check=True)
    else:
        print(f"[boot] branch {BOOT_BRANCH} not found on fork — creating from origin/main")
        subprocess.run(["git", "checkout", "-b", BOOT_BRANCH, "origin/main"], cwd=str(REPO_DIR), check=True)
        subprocess.run(["git", "push", "-u", "origin", BOOT_BRANCH], cwd=str(REPO_DIR), check=True)
        _stable = f"{BOOT_BRANCH}-stable"
        subprocess.run(["git", "branch", _stable], cwd=str(REPO_DIR), check=True)
        subprocess.run(["git", "push", "-u", "origin", _stable], cwd=str(REPO_DIR), check=True)

if (REPO_DIR / ".git").exists():
    HEAD_SHA = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(REPO_DIR), text=True).strip()
else:
    HEAD_SHA = ""
print(
    "[boot] branch=%s sha=%s worker_start=%s diag_heartbeat=%ss"
    % (
        BOOT_BRANCH,
        HEAD_SHA[:12],
        os.environ.get("DRAGO_WORKER_START_METHOD", ""),
        os.environ.get("DRAGO_DIAG_HEARTBEAT_SEC", ""),
    )
)

DRIVE_ROOT = _coerce_path(
    "DRAGO_DRIVE_ROOT",
    str(pathlib.Path(__file__).resolve().parent / ".drago_data"),
)
print(f"[boot] logs: {DRIVE_ROOT / 'logs' / 'supervisor.jsonl'}")

# Mount Drive in notebook process first (interactive auth works here).
if not DRAGO_LOCAL_MODE and drive is not None and not pathlib.Path("/content/drive/MyDrive").exists():
    drive.mount("/content/drive")

launcher_path = REPO_DIR / "colab_launcher.py"
assert launcher_path.exists(), f"Missing launcher: {launcher_path}"
subprocess.run([sys.executable, str(launcher_path)], cwd=str(REPO_DIR), check=True)
# Pass offline evolution flag explicitly if provided by user/launcher env.
os.environ["DRAGO_OFFLINE_EVOLUTION"] = "1" if DRAGO_OFFLINE_EVOLUTION else "0"
