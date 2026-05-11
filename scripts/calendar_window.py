"""
Restrict runs to a calendar date range and (optionally) a daily clock-time window.

Modes
-----
1) Gate only (exit 0 in window, 1 outside) — no --exec / no SCHED_RUN_CMD:
     python scripts/calendar_window.py && python tsp_solver.py ...

2) Run a command when in window (recommended for Task Scheduler):
     cd planet-tsp-solver
     python scripts/calendar_window.py --exec python tsp_solver.py --from-challenge-api \\
       --all-challenges --parallel 6 --submit --batch-summary \\
       --player-guid YOUR_GUID --player-email YOUR_EMAIL

   Prefer secrets via environment (Task Scheduler: task -> Environment):
     PLAYER_GUID, PLAYER_EMAIL
     then omit them from the command line:
     python scripts/calendar_window.py --exec python tsp_solver.py --from-challenge-api ...

3) Shell one-liner from env SCHED_RUN_CMD (optional SCHED_CWD):
     set SCHED_RUN_CMD=python tsp_solver.py ...
     python scripts/calendar_window.py

4) One log file per run (UTF-8):
     python scripts/calendar_window.py --log-dir C:\\logs\\tsp --exec python tsp_solver.py ...
     or  --log-file "C:\\logs\\run_{timestamp}.log"
     Placeholders: {timestamp} or {ts} -> YYYYMMDD_HHMMSS_microseconds (unique per run).
     Env: SCHED_LOG_DIR, SCHED_LOG_FILE (same {timestamp} / {ts} rules).

   Do not commit credentials to the repo.

Configuration (date / time)
---------------------------
  DEFAULT_* in this file, or env:
  SCHED_START_MONTH, SCHED_START_DAY, SCHED_END_MONTH, SCHED_END_DAY
  SCHED_ENFORCE_DAILY_TIME=1 and SCHED_DAILY_* for a daily local-time band.

GitHub Actions: set GITHUB_OUTPUT; script writes should_run= and does not subprocess.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date, datetime

# ---------------------------------------------------------------------------
# Defaults — edit here or override with env vars listed in the docstring.
# ---------------------------------------------------------------------------
DEFAULT_START_MONTH = 5
DEFAULT_START_DAY = 10
DEFAULT_END_MONTH = 5
DEFAULT_END_DAY = 17

# If True, also require "now" to be inside the daily interval (local time).
DEFAULT_ENFORCE_DAILY_TIME = True
DEFAULT_DAILY_START_HOUR = 21
DEFAULT_DAILY_START_MINUTE = 57
DEFAULT_DAILY_END_HOUR = 1
DEFAULT_DAILY_END_MINUTE = 59


def _parse_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _now_minute_of_day(now: datetime) -> int:
    return now.hour * 60 + now.minute


def _in_daily_time_window_minutes(now: datetime, start_min: int, end_min: int) -> bool:
    """Inclusive minute-of-day window; supports overnight if end_min < start_min."""
    n = _now_minute_of_day(now)
    if start_min <= end_min:
        return start_min <= n <= end_min
    return n >= start_min or n <= end_min


def _log_timestamp_token(now: datetime) -> str:
    return now.strftime("%Y%m%d_%H%M%S") + f"_{now.microsecond:06d}"


def _resolve_log_path(now: datetime, log_dir: str | None, log_file: str | None) -> str | None:
    """Unique path per run if using --log-dir or {timestamp}/{ts} in --log-file."""
    ts = _log_timestamp_token(now)
    if log_file and log_file.strip():
        path = log_file.strip().replace("{timestamp}", ts).replace("{ts}", ts)
        return path
    if log_dir and log_dir.strip():
        return os.path.join(log_dir.strip(), f"calendar_run_{ts}.log")
    return None


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def _evaluate_window(now: datetime) -> tuple[bool, str]:
    today = now.date()

    sm = _parse_int("SCHED_START_MONTH", DEFAULT_START_MONTH)
    sd = _parse_int("SCHED_START_DAY", DEFAULT_START_DAY)
    em = _parse_int("SCHED_END_MONTH", DEFAULT_END_MONTH)
    ed = _parse_int("SCHED_END_DAY", DEFAULT_END_DAY)

    start_d = date(today.year, sm, sd)
    end_d = date(today.year, em, ed)
    if start_d > end_d:
        return False, "calendar_window: SCHED_START_* must be on or before SCHED_END_*"

    date_ok = start_d <= today <= end_d

    enforce_time = _env_bool("SCHED_ENFORCE_DAILY_TIME", DEFAULT_ENFORCE_DAILY_TIME)
    sh = _parse_int("SCHED_DAILY_START_HOUR", DEFAULT_DAILY_START_HOUR)
    smin = _parse_int("SCHED_DAILY_START_MINUTE", DEFAULT_DAILY_START_MINUTE)
    eh = _parse_int("SCHED_DAILY_END_HOUR", DEFAULT_DAILY_END_HOUR)
    emin = _parse_int("SCHED_DAILY_END_MINUTE", DEFAULT_DAILY_END_MINUTE)
    start_min = sh * 60 + smin
    end_min = eh * 60 + emin

    time_ok = _in_daily_time_window_minutes(now, start_min, end_min) if enforce_time else True

    ok = date_ok and time_ok

    parts = [
        f"date={today.isoformat()} date_ok={date_ok} [{start_d.isoformat()} .. {end_d.isoformat()}]",
    ]
    if enforce_time:
        parts.append(
            f"time_ok={time_ok} local minute-of-day in [{start_min}..{end_min}] "
            f"(HH:MM {sh:02d}:{smin:02d} .. {eh:02d}:{emin:02d})"
        )
    else:
        parts.append("daily_time=not enforced (set SCHED_ENFORCE_DAILY_TIME=1 to require a daily window)")
    describe = " ".join(parts)
    return ok, describe


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Calendar / clock gate; optionally run a command when the window allows it."
    )
    ap.add_argument(
        "--cwd",
        default=os.environ.get("SCHED_CWD") or None,
        metavar="DIR",
        help="Working directory for the subprocess (default: SCHED_CWD env, else unchanged).",
    )
    ap.add_argument(
        "--exec",
        nargs=argparse.REMAINDER,
        metavar="ARG",
        help="When in window, run this argv (everything after --exec). Example: --exec python tsp_solver.py -h",
    )
    ap.add_argument(
        "--log-dir",
        default=(os.environ.get("SCHED_LOG_DIR") or "").strip() or None,
        metavar="DIR",
        help="Write a new UTF-8 log per run: DIR/calendar_run_<timestamp>.log",
    )
    ap.add_argument(
        "--log-file",
        default=(os.environ.get("SCHED_LOG_FILE") or "").strip() or None,
        metavar="PATH",
        help="Exact log path; use {timestamp} or {ts} for a unique name per run. Overrides --log-dir if both set.",
    )
    cli = ap.parse_args()

    exec_argv = cli.exec
    if exec_argv is not None:
        if len(exec_argv) == 0:
            exec_argv = None
        elif exec_argv[0] == "--":
            exec_argv = exec_argv[1:]
            if not exec_argv:
                exec_argv = None

    now = datetime.now()
    log_path = _resolve_log_path(now, cli.log_dir, cli.log_file)
    log_fp = None
    if log_path:
        try:
            _ensure_parent_dir(log_path)
            log_fp = open(log_path, "w", encoding="utf-8")
        except OSError as exc:
            print(f"calendar_window: could not open log {log_path}: {exc}", file=sys.stderr)
            return 2

    def log_line(msg: str, *, to_file: bool = True) -> None:
        print(msg, flush=True)
        if log_fp and to_file:
            log_fp.write(msg + "\n")
            log_fp.flush()

    ok, describe = _evaluate_window(now)
    if describe.startswith("calendar_window:"):
        print(describe, file=sys.stderr)
        if log_fp:
            log_fp.write(describe + "\n")
            log_fp.close()
        return 2

    gh_out = os.environ.get("GITHUB_OUTPUT")

    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"should_run={str(ok).lower()}\n")
        msg = f"calendar_window: {describe} -> should_run={ok} (Actions)"
        print(msg)
        if log_fp:
            log_fp.write(msg + "\n")
            log_fp.close()
        return 0

    if log_path:
        log_line(f"calendar_window: log file: {log_path}")
    log_line(f"calendar_window: {describe} -> in_window={ok}")

    if not ok:
        if log_fp:
            log_fp.write("calendar_window: skipped (outside window)\n")
            log_fp.close()
        return 1

    run_cwd = cli.cwd

    def _run_subprocess_logged(cmd: str | list[str], *, shell: bool = False) -> int:
        if not log_fp:
            r = subprocess.run(cmd, cwd=run_cwd, shell=shell)
            return int(r.returncode)
        shown = "(shell SCHED_RUN_CMD)" if shell else repr(cmd)
        log_line(f"calendar_window: subprocess {shown}")
        if shell:
            proc = subprocess.Popen(
                cmd,
                cwd=run_cwd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        else:
            proc = subprocess.Popen(
                cmd,
                cwd=run_cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            log_fp.write(line)
            log_fp.flush()
        return int(proc.wait())

    if exec_argv:
        code = _run_subprocess_logged(exec_argv, shell=False)
        if log_fp:
            log_fp.write(f"\ncalendar_window: subprocess exit code {code}\n")
            log_fp.close()
        return code

    shell_cmd = os.environ.get("SCHED_RUN_CMD", "").strip()
    if shell_cmd:
        code = _run_subprocess_logged(shell_cmd, shell=True)
        if log_fp:
            log_fp.write(f"\ncalendar_window: subprocess exit code {code}\n")
            log_fp.close()
        return code

    if log_fp:
        log_fp.write("calendar_window: no --exec and no SCHED_RUN_CMD (gate only)\n")
        log_fp.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
