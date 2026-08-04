#!/usr/bin/env python3
"""swarmd.py - workout-agent AI coding supervisor (the "swarm").

A single-process daemon that continuously drains a task queue
(`.tasks/{pending,active,stuck,completed}/*.task`) by handing each task to
one of several AI coding CLIs (Claude, Antigravity/Gemini, GitHub Copilot via
aider, DeepSeek via aider) in a rotating fallback chain, verifies the result
with a blocking lint/type/test gate, and commits+pushes verified work
straight to `main`. When the queue is empty it invents audit/review work so
it never runs out of things to do.

Modelled on the same pattern used for other repos in this workspace, adapted
for this project's Python/FastAPI/pytest/ruff stack. See AGENTS.md and
SWARM.md for the operational rules this daemon enforces mechanically.

Usage:
    ./swarmd.py                 run the supervisor loop forever
    ./swarmd.py --once          run a single cycle then exit (useful for cron/CI)
    ./swarmd.py --status        print current state and exit
    ./swarmd.py --tasks         print task queue counts and exit
    ./swarmd.py --migrate       migrate legacy TODO-style items into .tasks/pending/
    ./swarmd.py --relaxed-tests run with the verification gate downgraded to advisory
"""

from __future__ import annotations

import difflib
import fcntl
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths & config
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent
TASKS_DIR = REPO_ROOT / ".tasks"
LOG_DIR = REPO_ROOT / "logs"
STATE_FILE = Path(os.environ.get("SWARM_STATE_FILE", "/tmp/workout_agent_swarm_state.json"))
LOCK_FILE = Path(os.environ.get("SWARM_LOCK_FILE", "/tmp/workout_agent_swarm_coordination.lock"))
HEARTBEAT_DIR = Path("/tmp/workout_agent_swarm_watchdog")
HEARTBEAT_FILE = HEARTBEAT_DIR / "heartbeat"

for d in (TASKS_DIR / "pending", TASKS_DIR / "active", TASKS_DIR / "stuck",
          TASKS_DIR / "completed", LOG_DIR, HEARTBEAT_DIR):
    d.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "swarmd.log"),
    ],
)
log = logging.getLogger("swarmd")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() not in ("", "0", "false", "no")


CFG: dict[str, Any] = {
    "max_retries": _env_int("SWARM_MAX_RETRIES", 2),
    "cooldown": _env_int("SWARM_COOLDOWN", 15),
    "stuck_timeout": _env_int("SWARM_STUCK_TIMEOUT", 300),
    "test_timeout": _env_int("SWARM_TEST_TIMEOUT", 180),
    "ai_rate_cooldown": _env_int("AI_RATE_COOLDOWN_SECONDS", 15),
    "discover_timeout": _env_int("SWARM_DISCOVER_TIMEOUT", 180),
    "gh_sync_cycles": _env_int("SWARM_GH_SYNC_CYCLES", 20),  # minutes
    "review_cycles": _env_int("SWARM_REVIEW_CYCLES", 5),
    "fix_max_rounds": _env_int("SWARM_FIX_MAX_ROUNDS", 5),
    "stuck_alert_seconds": _env_int("SWARM_STUCK_ALERT_SECONDS", 900),
    "stuck_restart_seconds": _env_int("SWARM_STUCK_RESTART_SECONDS", 2700),
    "relaxed_tests": _env_bool("SWARM_RELAXED_TESTS", False),
    "models": os.environ.get("SWARM_MODEL_ORDER", "claude,antigravity,copilot,deepseek").split(","),
    "repo_owner": os.environ.get("SWARM_REPO_OWNER", "elgansayer"),
    "repo_name": os.environ.get("SWARM_REPO_NAME", "workout-agent"),
    "git_remote": os.environ.get("SWARM_GIT_REMOTE", "origin"),
    "git_branch": os.environ.get("SWARM_GIT_BRANCH", "main"),
    "git_user": os.environ.get("SWARM_GIT_USER", "AI Swarm"),
    "git_email": os.environ.get("SWARM_GIT_EMAIL", "swarm@workout-agent.local"),
}

if "--relaxed-tests" in sys.argv:
    CFG["relaxed_tests"] = True

_shutdown = False


def _on_signal(signum: int, frame: Any) -> None:
    global _shutdown
    log.warning("Received signal %s, shutting down after current step...", signum)
    _shutdown = True


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def state_load() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"tasks_completed": 0, "tasks_stuck": 0, "cycles": 0, "started_at": time.time()}


def state_save(state: dict[str, Any]) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.rename(STATE_FILE)


def heartbeat() -> None:
    HEARTBEAT_FILE.write_text(str(time.time()))


# ---------------------------------------------------------------------------
# Environment validation
# ---------------------------------------------------------------------------

MODEL_REQUIREMENTS: dict[str, dict[str, str | None]] = {
    "claude": {"binary": "claude", "env": None},
    "antigravity": {"binary": "agy", "env": "GEMINI_API_KEY"},
    "copilot": {"binary": "aider", "env": "OPENAI_API_KEY"},
    "deepseek": {"binary": "aider", "env": "DEEPSEEK_API_KEY"},
}


def _binary_available(name: str) -> str | None:
    from shutil import which
    path = which(name)
    if path:
        return path
    local = Path.home() / ".local" / "bin" / name
    if local.exists():
        return str(local)
    return None


def validate_environment() -> list[str]:
    """Return the list of model names actually usable right now."""
    available = []
    for model in CFG["models"]:
        model = model.strip()
        req = MODEL_REQUIREMENTS.get(model)
        if req is None:
            log.warning("Unknown model '%s' in SWARM_MODEL_ORDER, skipping", model)
            continue
        binary_name = req["binary"]
        assert binary_name is not None
        binpath = _binary_available(binary_name)
        env_name = req["env"]
        has_key = env_name is None or bool(os.environ.get(env_name))
        if binpath and has_key:
            available.append(model)
            log.info("Model '%s': READY (%s)", model, binpath)
        else:
            reason = []
            if not binpath:
                reason.append(f"binary '{req['binary']}' not found")
            if not has_key:
                reason.append(f"{req['env']} not set")
            log.warning("Model '%s': UNAVAILABLE (%s)", model, ", ".join(reason))
    if not available:
        log.error("FATAL: no AI models are available. Set at least one of: %s",
                   ", ".join(f"{m}({r['env'] or 'no key needed'})" for m, r in MODEL_REQUIREMENTS.items()))
    return available


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=check)


def _git_status_snapshot() -> str:
    return subprocess.run(["git", "status", "--porcelain"], cwd=REPO_ROOT,
                           capture_output=True, text=True, check=False).stdout


def _git_real_changes(before: str, after: str) -> int:
    """Count changed paths, excluding noise (logs, aider scratch files)."""
    def paths(snapshot: str) -> set[str]:
        out = set()
        for line in snapshot.splitlines():
            p = line[3:].strip()
            if p and not p.endswith((".log", ".aider.chat.history.md", ".aider.input.history")) \
                    and "STUCK_LOG" not in p and "TODO.md" not in p:
                out.add(p)
        return out
    return len(paths(after) - paths(before) | paths(before).symmetric_difference(paths(after)))


def discard_working_tree_changes() -> None:
    """Throw away all uncommitted changes (used only when a task truly fails)."""
    log.warning("Discarding working tree changes")
    _git("checkout", "--", ".", check=False)
    subprocess.run(
        ["git", "clean", "-fd",
         "-e", "data", "-e", ".env", "-e", ".venv", "-e", "__pycache__", "-e", ".tasks"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    )


def git_commit(message: str, push: bool = True) -> bool:
    _git("add", "-A")
    status = _git("status", "--porcelain").stdout
    if not status.strip():
        return False
    env_args = ["-c", f"user.name={CFG['git_user']}", "-c", f"user.email={CFG['git_email']}"]
    result = subprocess.run(["git", *env_args, "commit", "-m", message],
                             cwd=REPO_ROOT, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        log.error("git commit failed: %s", result.stderr[:500])
        return False
    if not push:
        return True
    push_result = subprocess.run(["git", "push", CFG["git_remote"], CFG["git_branch"]],
                                  cwd=REPO_ROOT, capture_output=True, text=True, check=False)
    if push_result.returncode == 0:
        return True
    log.warning("push rejected, attempting rebase: %s", push_result.stderr[:300])
    subprocess.run(["git", "pull", "--rebase", CFG["git_remote"], CFG["git_branch"]],
                    cwd=REPO_ROOT, capture_output=True, text=True, check=False)
    retry = subprocess.run(["git", "push", CFG["git_remote"], CFG["git_branch"]],
                            cwd=REPO_ROOT, capture_output=True, text=True, check=False)
    if retry.returncode != 0:
        log.error("push failed after rebase: %s", retry.stderr[:500])
        return False
    return True


# ---------------------------------------------------------------------------
# Task queue
# ---------------------------------------------------------------------------

def _slugify(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "task"


def _all_task_titles() -> list[tuple[str, Path]]:
    out = []
    for state in ("pending", "active", "stuck", "completed"):
        for f in (TASKS_DIR / state).glob("*.task"):
            try:
                title = f.read_text().strip().split("\n")[0]
            except OSError:
                continue
            out.append((title, f))
    return out


def find_similar_task(description: str, threshold: float = 0.72) -> Path | None:
    first_line = description.strip().split("\n")[0]
    for title, path in _all_task_titles():
        ratio = difflib.SequenceMatcher(None, first_line.lower(), title.lower()).ratio()
        if ratio >= threshold:
            return path
    return None


def task_add(description: str, phase: str = "0000") -> Path | None:
    if find_similar_task(description):
        log.info("Skipping near-duplicate task: %s", description.split("\n")[0][:80])
        return None
    existing = sorted((TASKS_DIR / "pending").glob(f"{phase}-*.task"))
    seq = len(existing) + 1
    slug = _slugify(description.split("\n")[0])
    fname = f"{phase}-{seq:03d}-{slug}.task"
    path = TASKS_DIR / "pending" / fname
    path.write_text(description.strip() + "\n")
    log.info("Queued task: %s", fname)
    return path


def task_next() -> tuple[str | None, Path | None]:
    for state in ("active", "pending"):
        tasks = sorted((TASKS_DIR / state).glob("*.task"))
        if tasks:
            f = tasks[0]
            content = f.read_text().strip()
            return content, f
    return None, None


def task_move(taskfile: Path, to_state: str) -> Path:
    dest = TASKS_DIR / to_state / taskfile.name
    taskfile.rename(dest)
    return dest


def task_stats() -> dict[str, int]:
    return {s: len(list((TASKS_DIR / s).glob("*.task")))
            for s in ("pending", "active", "stuck", "completed")}


# ---------------------------------------------------------------------------
# GitHub issue sync (optional - requires `gh` CLI authenticated)
# ---------------------------------------------------------------------------

def _gh_available() -> bool:
    return _binary_available("gh") is not None


def sync_github_issues() -> None:
    if not _gh_available():
        return
    try:
        result = subprocess.run(
            ["gh", "issue", "list", "--repo", f"{CFG['repo_owner']}/{CFG['repo_name']}",
             "--state", "open", "--label", "ai-agent-task",
             "--json", "number,title", "--limit", "200"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode != 0:
            log.debug("gh issue list failed (probably no remote issues yet): %s", result.stderr[:200])
            return
        issues = json.loads(result.stdout or "[]")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        log.warning("sync_github_issues failed: %s", exc)
        return

    for issue in issues:
        number, title = issue["number"], issue["title"]
        if find_similar_task(title):
            continue
        slug = _slugify(title)
        fname = f"{number:05d}-001-{slug}.task"
        if any((TASKS_DIR / s / fname).exists() for s in ("pending", "active", "stuck", "completed")):
            continue
        (TASKS_DIR / "pending" / fname).write_text(title + "\n")
        log.info("Imported GitHub issue #%s as task", number)


def close_github_issue_for_task(taskfile: Path) -> None:
    if not _gh_available():
        return
    match = re.match(r"(\d{5})-", taskfile.name)
    if not match or match.group(1) == "00000":
        return
    number = int(match.group(1))
    subprocess.run(
        ["gh", "issue", "close", str(number), "--repo", f"{CFG['repo_owner']}/{CFG['repo_name']}",
         "--comment", "Closed automatically by swarmd.py after verified completion."],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=20, check=False,
    )


# ---------------------------------------------------------------------------
# Process liveness monitoring (shared by all model runners)
# ---------------------------------------------------------------------------

def _run_process_live(cmd: list[str], *, cwd: Path = REPO_ROOT, stuck_timeout: int | None = None,
                       env: dict[str, str] | None = None) -> tuple[int, str, bool]:
    """Run a subprocess, killing it only if BOTH stdout is silent AND the
    working tree is unchanged for `stuck_timeout` seconds. No hard wall-clock
    timeout - a model gets to keep working as long as it's producing output
    or changing files."""
    stuck_timeout = stuck_timeout or CFG["stuck_timeout"]
    full_env = {**os.environ, **(env or {})}
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, env=full_env, bufsize=1)
    output: list[str] = []
    last_output_ts = time.time()
    lock = threading.Lock()

    def reader() -> None:
        nonlocal last_output_ts
        assert proc.stdout is not None
        for line in proc.stdout:
            with lock:
                output.append(line)
                last_output_ts = time.time()

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    killed = False
    last_git = _git_status_snapshot()
    last_progress_ts = time.time()
    while proc.poll() is None:
        time.sleep(5)
        with lock:
            silent_for = time.time() - last_output_ts
        current_git = _git_status_snapshot()
        if current_git != last_git:
            last_git = current_git
            last_progress_ts = time.time()
        stalled_for = time.time() - last_progress_ts
        if silent_for > stuck_timeout and stalled_for > stuck_timeout:
            log.warning("Process stalled (%ss silent, %ss no file changes), terminating", int(silent_for), int(stalled_for))
            proc.terminate()
            time.sleep(2)
            if proc.poll() is None:
                proc.kill()
            killed = True
            break
        if _shutdown:
            proc.terminate()
            break

    t.join(timeout=5)
    return proc.returncode or 0, "".join(output), killed


def _constitution_prefix() -> str:
    return (
        "Follow AGENTS.md in the repository root strictly (multi-tenancy, "
        "AI-provider wiring, verification-gate, and skills-system rules). "
        "Consult .agents/skills/ for the relevant skill before implementing. "
        "Task:\n\n"
    )


# ---------------------------------------------------------------------------
# Model runners
# ---------------------------------------------------------------------------

def run_claude(task: str) -> tuple[bool, str]:
    prompt = _constitution_prefix() + task
    before = _git_status_snapshot()
    code, out, killed = _run_process_live(
        ["claude", "-p", "--dangerously-skip-permissions", prompt]
    )
    after = _git_status_snapshot()
    ok = _git_real_changes(before, after) > 0 or (code == 0 and not killed and "audit" in task.lower())
    return ok, out


def run_antigravity(task: str) -> tuple[bool, str]:
    prompt = _constitution_prefix() + task
    binpath = _binary_available("agy") or _binary_available("antigravity")
    if not binpath:
        return False, "antigravity binary not found"
    before = _git_status_snapshot()
    code, out, killed = _run_process_live([binpath, "-p", "--dangerously-skip-permissions", prompt])
    after = _git_status_snapshot()
    ok = _git_real_changes(before, after) > 0 or (code == 0 and not killed and "audit" in task.lower())
    return ok, out


def run_copilot(task: str) -> tuple[bool, str]:
    """GitHub Copilot's OpenAI-compatible endpoint, driven via aider."""
    prompt = _constitution_prefix() + task
    before = _git_status_snapshot()
    cmd = [
        "aider", "--model", "openai/gpt-4o",
        "--openai-api-base", "https://api.githubcopilot.com",
        "--read", "AGENTS.md",
        "--message", prompt,
        "--no-auto-commits", "--yes-always", "--no-check-update",
    ]
    _code, out, _killed = _run_process_live(cmd, env={"OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", "")})
    after = _git_status_snapshot()
    ok = _git_real_changes(before, after) > 0
    return ok, out


def run_deepseek(task: str) -> tuple[bool, str]:
    """Two-pass aider workflow: discover files, then edit them."""
    prompt = _constitution_prefix() + task
    model = os.environ.get("AIDER_MODEL", "deepseek/deepseek-reasoner")
    env = {"DEEPSEEK_API_KEY": os.environ.get("DEEPSEEK_API_KEY", "")}

    discover_cmd = [
        "aider", "--model", model, "--read", "AGENTS.md",
        "--message", "List (do not edit) the files you would need to change for this task, "
                      "one path per line:\n\n" + prompt,
        "--no-auto-commits", "--yes-always", "--no-check-update",
    ]
    _, discover_out, _ = _run_process_live(discover_cmd, stuck_timeout=CFG["discover_timeout"], env=env)

    files: set[str] = set()
    for match in re.finditer(r"\b((?:frontend|backend|webapp|tests)/[\w./-]+\.\w+|\b[\w-]+\.py\b)", discover_out):
        candidate = REPO_ROOT / match.group(1)
        if candidate.exists():
            files.add(match.group(1))
    files = set(list(files)[:30])

    before = _git_status_snapshot()
    edit_cmd = ["aider", "--model", model, "--read", "AGENTS.md"]
    for f in files:
        edit_cmd += ["--file", f]
    edit_cmd += ["--message", prompt, "--no-auto-commits", "--yes-always", "--no-check-update"]
    _code, out, _killed = _run_process_live(edit_cmd, env=env)
    after = _git_status_snapshot()
    ok = _git_real_changes(before, after) > 0
    return ok, discover_out + "\n" + out


MODEL_RUNNERS: dict[str, Callable[[str], tuple[bool, str]]] = {
    "claude": run_claude,
    "antigravity": run_antigravity,
    "copilot": run_copilot,
    "deepseek": run_deepseek,
}

_last_ai_call_ts = 0.0


def run_task_with_fallback(task: str, available_models: list[str], cycle: int) -> tuple[bool, str, str]:
    global _last_ai_call_ts
    if not available_models:
        return False, "", "no models available"
    start = cycle % len(available_models)
    ordered = available_models[start:] + available_models[:start]
    last_output = ""
    for model in ordered:
        elapsed = time.time() - _last_ai_call_ts
        if elapsed < CFG["ai_rate_cooldown"]:
            time.sleep(CFG["ai_rate_cooldown"] - elapsed)
        _last_ai_call_ts = time.time()
        log.info("Attempting task with model=%s", model)
        try:
            ok, output = MODEL_RUNNERS[model](task)
        except Exception as exc:
            log.exception("Runner '%s' crashed", model)
            ok, output = False, str(exc)
        last_output = output
        if ok:
            return True, output, model
        log.warning("Model '%s' produced no usable change, trying next", model)
        time.sleep(3)
    return False, last_output, "none"


# ---------------------------------------------------------------------------
# Verification gate
# ---------------------------------------------------------------------------

VERIFICATION_STEPS: list[tuple[str, list[str]]] = [
    ("syntax", [sys.executable, "-m", "compileall", "-q", "."]),
    ("ruff", [sys.executable, "-m", "ruff", "check", "."]),
    ("pytest", [sys.executable, "-m", "pytest", "-q"]),
    ("webapp-import", [sys.executable, "-c", "import webapp.app"]),
    ("agent-import", [sys.executable, "-c", "import main"]),
]


def run_tests(only_checks: set[str] | None = None) -> tuple[bool, dict[str, str]]:
    errors: dict[str, str] = {}
    all_ok = True
    for name, cmd in VERIFICATION_STEPS:
        if only_checks is not None and name not in only_checks:
            continue
        try:
            result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True,
                                     timeout=CFG["test_timeout"], check=False)
        except subprocess.TimeoutExpired:
            all_ok = False
            errors[name] = f"TIMEOUT after {CFG['test_timeout']}s"
            continue
        if result.returncode != 0:
            all_ok = False
            errors[name] = (result.stdout[-2000:] + "\n" + result.stderr[-1000:])
    return all_ok, errors


def run_test_fix(errors: dict[str, str], available_models: list[str], cycle: int) -> bool:
    """Attempt to fix failing checks, iterating up to fix_max_rounds times."""
    failed_checks = set(errors.keys())
    for round_num in range(CFG["fix_max_rounds"]):
        file_hints: set[str] = set()
        for text in errors.values():
            file_hints.update(re.findall(r"\b([\w./-]+\.py):\d+", text))
        file_hints = set(list(file_hints)[:10])
        detail = "\n\n".join(f"### {name}\n{text[:1500]}" for name, text in errors.items())
        fix_task = (
            "Fix the following verification failures. Do not weaken tests or "
            "gate checks to make them pass - fix the underlying code.\n\n"
            f"Files most likely involved: {', '.join(sorted(file_hints)) or 'unknown - inspect the errors below'}\n\n"
            f"{detail}"
        )
        log.info("Fix round %d/%d for: %s", round_num + 1, CFG["fix_max_rounds"], ", ".join(failed_checks))
        ok, _, _ = run_task_with_fallback(fix_task, available_models, cycle + round_num)
        if not ok:
            continue
        passed, errors = run_tests(only_checks=failed_checks)
        if passed:
            return True
        failed_checks = set(errors.keys())
    return False


# ---------------------------------------------------------------------------
# Self-review (post-commit static checks -> follow-up tasks)
# ---------------------------------------------------------------------------

def self_review(commit_sha: str) -> None:
    diff = subprocess.run(["git", "show", "--unified=0", commit_sha], cwd=REPO_ROOT,
                           capture_output=True, text=True, check=False).stdout
    findings: list[str] = []

    new_py_files = re.findall(r"^\+\+\+ b/(.*\.py)$", diff, re.MULTILINE)
    for f in new_py_files:
        if f.startswith("tests/") or f.endswith("_test.py"):
            continue
        stem = Path(f).stem
        has_test = any((REPO_ROOT / "tests" / name).exists()
                        for name in (f"test_{stem}.py",))
        if not has_test:
            findings.append(f"Code review finding: add tests/test_{stem}.py covering the new module {f} (from commit {commit_sha[:8]})")

    if re.search(r"^\+.*except\s*:\s*$", diff, re.MULTILINE):
        findings.append(f"Code review finding: replace bare 'except:' with a specific exception type (from commit {commit_sha[:8]})")

    if re.search(r'^\+.*(api_key|client_secret|refresh_token)\s*=.*(logging\.|print\()', diff, re.MULTILINE):
        findings.append(f"SECURITY: possible secret logged in commit {commit_sha[:8]} - review and redact")

    if re.search(r"^\+.*f\"[^\"]*\{[^}]+\}[^\"]*\"\s*\)?\s*$", diff, re.MULTILINE) and \
       re.search(r"cursor\.execute|\.execute\(", diff):
        findings.append(f"SECURITY: possible SQL built via f-string near .execute() in commit {commit_sha[:8]} - verify parameterised placeholders are used")

    for finding in findings:
        task_add(finding)


# ---------------------------------------------------------------------------
# Perpetual work generator
# ---------------------------------------------------------------------------

AUDIT_PROMPTS = [
    "Audit database.py for any table still missing a user_id column and migrate the single highest-traffic one following the multi-tenant-migration skill.",
    "Audit webapp/app.py routes for missing auth-middleware coverage or endpoints that bypass the rate limiter, per the daily security audit automation.",
    "Audit every AI-generation call site (gemini_engine.py, insight_cron.py, webapp/app.py chat/RAG/XAI endpoints) for hardcoded Gemini usage that should resolve the user's preferred provider via ai_provider.py, per the ai-provider-wiring skill.",
    "Audit whether programme_inference.py and hevy_reader.py are wired into main.py/webapp/app.py yet; if still orphaned, wire in the least risky integration point.",
    "Audit test coverage gaps listed in AGENTS.md Known Issues and add tests for the module with the highest risk-to-coverage ratio (encryption.py, ai_provider.py, or gemini_engine.py first).",
    "Audit README.md, .env.example, and docker-compose*.yml for drift against actual code (port numbers, login behaviour, documented env vars) per the daily documentation sync automation.",
    "Audit webapp/templates/*.html for dead buttons, broken links, or empty-state handling gaps per the weekly UX audit automation.",
    "Deep code review of swarmd.py itself: check for bugs, unhandled exceptions in model runners, and verification-gate correctness. Harden anything fragile.",
    "Audit connector modules (hevy_client.py, google_health_client.py, health_connect.py, weather.py, telegram_notifier.py) for network calls that could crash the caller on failure, per the connector-integration skill.",
    "Audit requirements.txt and requirements-web.txt for outdated or vulnerable pinned dependencies and bump one safely, verifying the full gate still passes.",
    "Review the programme-builder-ui skill and scope the next concrete step toward letting a user select or build their own workout programme from the dashboard.",
    "Review the scheduler-job skill and scope the next concrete step toward consolidating docker-entrypoint.sh's and insight_scheduler.py's duplicate sleep loops.",
]


def generate_review_task() -> None:
    idx = int(time.time() / 3600) % len(AUDIT_PROMPTS)
    task_add(AUDIT_PROMPTS[idx])


# ---------------------------------------------------------------------------
# Telegram alerting (optional, reuses the app's own bot token if configured)
# ---------------------------------------------------------------------------

def send_telegram_alert(message: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("SWARM_ALERT_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        import urllib.parse
        import urllib.request
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": f"[swarmd] {message}"}).encode()
        urllib.request.urlopen(
            urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data),
            timeout=10,
        )
    except Exception:
        log.exception("Failed to send Telegram alert")


# ---------------------------------------------------------------------------
# Supervisor loop
# ---------------------------------------------------------------------------

def supervisor(once: bool = False) -> None:
    LOCK_FILE.touch(exist_ok=True)
    lock_fd = os.open(str(LOCK_FILE), os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.error("Another swarmd.py instance is already running (lock: %s). Exiting.", LOCK_FILE)
        sys.exit(1)

    log.info("=" * 60)
    log.info("workout-agent swarmd starting (relaxed_tests=%s)", CFG["relaxed_tests"])
    available_models = validate_environment()
    if not available_models:
        sys.exit(1)
    log.info("Available models (fallback order rotates by cycle): %s", available_models)

    state = state_load()
    last_gh_sync = 0.0
    last_progress_ts = time.time()
    stuck_alerted = False

    sync_github_issues()

    while not _shutdown:
        heartbeat()
        cycle = state.get("cycles", 0)

        if time.time() - last_progress_ts > CFG["stuck_restart_seconds"]:
            log.error("No progress for %ss - forcing recovery restart", CFG["stuck_restart_seconds"])
            send_telegram_alert(f"No progress for {CFG['stuck_restart_seconds']}s, forcing recovery restart")
            discard_working_tree_changes()
            last_progress_ts = time.time()
            stuck_alerted = False
        elif time.time() - last_progress_ts > CFG["stuck_alert_seconds"] and not stuck_alerted:
            send_telegram_alert(f"No progress for {CFG['stuck_alert_seconds']}s")
            stuck_alerted = True

        if time.time() - last_gh_sync > CFG["gh_sync_cycles"] * 60:
            sync_github_issues()
            last_gh_sync = time.time()

        task, taskfile = task_next()

        if task is None or taskfile is None:
            passed, errors = run_tests()
            if not passed:
                detail = "\n\n".join(f"### {n}\n{t[:800]}" for n, t in errors.items())
                task_add(f"Fix failing verification gate (found during idle regression check):\n\n{detail}")
            elif state.get("tasks_completed", 0) % CFG["review_cycles"] == 0:
                generate_review_task()
            state["cycles"] = cycle + 1
            state_save(state)
            if once:
                break
            time.sleep(CFG["cooldown"])
            continue

        if taskfile.parent.name == "pending":
            taskfile = task_move(taskfile, "active")
        log.info("Working task: %s", task.split("\n")[0][:100])

        succeeded = False
        last_model = "none"
        for attempt in range(CFG["max_retries"]):
            ok, _output, model = run_task_with_fallback(task, available_models, cycle + attempt)
            last_model = model
            if not ok:
                log.warning("Attempt %d/%d produced no changes", attempt + 1, CFG["max_retries"])
                continue

            if CFG["relaxed_tests"]:
                succeeded = True
                break

            passed, errors = run_tests()
            if passed:
                succeeded = True
                break

            log.warning("Verification gate failed after model=%s, attempting automated fix", model)
            if run_test_fix(errors, available_models, cycle):
                succeeded = True
                break
            log.warning("Automated fix did not resolve the gate, discarding and retrying task")
            discard_working_tree_changes()

        if succeeded:
            title = task.split("\n")[0][:72]
            if git_commit(f"feat: {title}", push=True):
                sha = _git("rev-parse", "HEAD").stdout.strip()
                log.info("Committed and pushed %s (model=%s)", sha[:8], last_model)
                self_review(sha)
            task_move(taskfile, "completed")
            close_github_issue_for_task(taskfile)
            state["tasks_completed"] = state.get("tasks_completed", 0) + 1
            last_progress_ts = time.time()
            stuck_alerted = False
        else:
            log.error("Task exhausted all retries and models, parking as stuck: %s", taskfile.name)
            discard_working_tree_changes()
            task_move(taskfile, "stuck")
            state["tasks_stuck"] = state.get("tasks_stuck", 0) + 1
            send_telegram_alert(f"Task stuck: {task.splitlines()[0][:100]}")

        state["cycles"] = cycle + 1
        state_save(state)
        if once:
            break
        time.sleep(CFG["cooldown"])

    log.info("Shutdown complete. Final state: %s", state)
    fcntl.flock(lock_fd, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_status() -> None:
    state = state_load()
    stats = task_stats()
    print(json.dumps({"state": state, "tasks": stats}, indent=2))


def cmd_tasks() -> None:
    stats = task_stats()
    for name, count in stats.items():
        print(f"{name:10s} {count}")


def cmd_migrate() -> None:
    """Import checkbox items from TODO-style markdown files into .tasks/pending/."""
    for candidate in (REPO_ROOT / "TODO.md", REPO_ROOT / "CONTINUE.md"):
        if not candidate.exists():
            continue
        for line in candidate.read_text().splitlines():
            match = re.match(r"^\s*-\s*\[ \]\s*(.+)$", line)
            if match:
                task_add(match.group(1).strip())
    print("Migration complete. See .tasks/pending/")


def main() -> None:
    if "--status" in sys.argv:
        cmd_status()
        return
    if "--tasks" in sys.argv:
        cmd_tasks()
        return
    if "--migrate" in sys.argv:
        cmd_migrate()
        return
    if "--once" in sys.argv:
        supervisor(once=True)
        return
    supervisor()


if __name__ == "__main__":
    main()
