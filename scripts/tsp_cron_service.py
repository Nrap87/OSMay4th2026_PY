#!/usr/bin/env python3
"""
Run tsp_solver batch (API challenges + submit) using APScheduler.

Install:
  pip install apscheduler

Modes
-----
1) Daily once (default): --hour / --minute at local time.
2) Every minute inside a wall-clock window (supports overnight):
     python scripts/tsp_cron_service.py --every-minute \\
       --window-start 22:35 --window-end 01:00 --log-dir C:\\logs\\tsp
   Runs at 22:35, 22:36, ... 23:59, 00:00, ... 01:00 (inclusive by minute).
   By default the process exits automatically once local time leaves the window
   (after any in-flight batch finishes). Use --no-exit-after-window to keep running.

Credentials: PLAYER_GUID, PLAYER_EMAIL (env). Optional: STAR_DELIVERY_BASE_URL.

Logs: one UTF-8 file per batch run (skipped minutes produce no log).

Env: TSP_SCHED_CRON_HOUR / TSP_SCHED_CRON_MINUTE (daily mode)
     TSP_SCHED_WINDOW_START / TSP_SCHED_WINDOW_END as HH:MM (every-minute mode)
     TSP_SCHED_LOG_DIR, TSP_SCHED_PARALLEL
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOLVER = ROOT / "tsp_solver.py"


def _parse_hhmm(label: str, s: str) -> tuple[int, int]:
    s = s.strip()
    parts = s.split(":")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"{label} must be HH:MM, got {s!r}")
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{label}: invalid number in {s!r}") from exc
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise argparse.ArgumentTypeError(f"{label}: hour 0-23, minute 0-59, got {s!r}")
    return h, m


def _now_minute_of_day(now: datetime) -> int:
    return now.hour * 60 + now.minute


def in_time_window(
    now: datetime,
    start_h: int,
    start_m: int,
    end_h: int,
    end_m: int,
) -> bool:
    """Inclusive minute-of-day window; if end < start on the clock, treat as overnight."""
    sm = start_h * 60 + start_m
    em = end_h * 60 + end_m
    n = _now_minute_of_day(now)
    if sm <= em:
        return sm <= n <= em
    return n >= sm or n <= em


def run_batch(log_dir: Path, parallel: int) -> None:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"tsp_schedule_{ts}.log"

    cmd = [
        sys.executable,
        str(SOLVER),
        "--from-challenge-api",
        "--all-challenges",
        f"--parallel={parallel}",
        "--submit",
        "--batch-summary",
    ]
    guid = os.environ.get("PLAYER_GUID", "").strip()
    email = os.environ.get("PLAYER_EMAIL", "").strip()
    if guid:
        cmd.append(f"--player-guid={guid}")
    if email:
        cmd.append(f"--player-email={email}")

    print(f"[{datetime.now().isoformat()}] starting batch -> {log_path}", flush=True)

    with open(log_path, "w", encoding="utf-8") as logf:
        logf.write(f"start {datetime.now().isoformat()}\n")
        logf.write(f"cwd: {ROOT}\n")
        logf.write(f"cmd: {subprocess.list2cmdline(cmd)}\n\n")
        logf.flush()

        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            logf.write(line)
            logf.flush()
            sys.stdout.write(line)
        code = proc.wait()
        logf.write(f"\nend {datetime.now().isoformat()} exit={code}\n")

    print(f"[{datetime.now().isoformat()}] finished exit={code} log={log_path}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="APScheduler: tsp_solver batch + submit; daily or every minute in a time window."
    )
    parser.add_argument("--hour", type=int, default=int(os.environ.get("TSP_SCHED_CRON_HOUR", "12")), help="Daily mode: cron hour (local)")
    parser.add_argument("--minute", type=int, default=int(os.environ.get("TSP_SCHED_CRON_MINUTE", "0")), help="Daily mode: cron minute (local)")
    parser.add_argument(
        "--every-minute",
        action="store_true",
        help="Run the job every clock minute, but only when local time is inside --window-start/--window-end.",
    )
    parser.add_argument(
        "--window-start",
        type=str,
        default=os.environ.get("TSP_SCHED_WINDOW_START", "").strip() or None,
        metavar="HH:MM",
        help="Every-minute mode: window start (local), e.g. 22:35. Env: TSP_SCHED_WINDOW_START",
    )
    parser.add_argument(
        "--window-end",
        type=str,
        default=os.environ.get("TSP_SCHED_WINDOW_END", "").strip() or None,
        metavar="HH:MM",
        help="Every-minute mode: window end inclusive by minute, e.g. 01:00. Env: TSP_SCHED_WINDOW_END",
    )
    parser.add_argument(
        "--no-exit-after-window",
        action="store_true",
        help="Every-minute mode: do not exit when the window ends; idle until Ctrl+C (legacy behavior).",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=None,
        help="Directory for run logs (default: TSP_SCHED_LOG_DIR or <repo>/logs)",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=int(os.environ.get("TSP_SCHED_PARALLEL", "6")),
        help="Parallel solves (default: 6 or TSP_SCHED_PARALLEL)",
    )
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="Run one batch immediately and exit (ignores window and scheduler).",
    )
    args = parser.parse_args()

    log_dir = args.log_dir
    if log_dir is None:
        raw = os.environ.get("TSP_SCHED_LOG_DIR", "").strip()
        log_dir = Path(raw) if raw else ROOT / "logs"

    if args.run_once:
        run_batch(log_dir, args.parallel)
        return 0

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:
        print("Install APScheduler: pip install apscheduler", file=sys.stderr)
        return 1

    if args.every_minute:
        if not args.window_start or not args.window_end:
            print(
                "--every-minute requires --window-start HH:MM and --window-end HH:MM "
                "(or TSP_SCHED_WINDOW_START / TSP_SCHED_WINDOW_END).",
                file=sys.stderr,
            )
            return 2
        sh, sm = _parse_hhmm("window-start", args.window_start)
        eh, em = _parse_hhmm("window-end", args.window_end)

        exit_after = not args.no_exit_after_window
        stop_event = threading.Event()
        state: dict[str, bool] = {"entered": False, "running": False}

        def maybe_run() -> None:
            now = datetime.now()
            in_w = in_time_window(now, sh, sm, eh, em)
            if in_w:
                state["entered"] = True
                state["running"] = True
                try:
                    run_batch(log_dir, args.parallel)
                finally:
                    state["running"] = False
            elif exit_after and state["entered"] and not state["running"]:
                stop_event.set()

        scheduler = BackgroundScheduler()
        scheduler.add_job(
            maybe_run,
            "cron",
            minute="*",
            second=0,
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        span = "same calendar day" if sh * 60 + sm <= eh * 60 + em else "overnight (wraps past midnight)"
        extra = (
            " Exits automatically after the window ends (when no batch is running)."
            if exit_after
            else " Runs until Ctrl+C."
        )
        print(
            f"Scheduler started: every minute at :00s, only between "
            f"{args.window_start} and {args.window_end} inclusive ({span}, local). "
            f"Logs -> {log_dir}.{extra}",
            flush=True,
        )
    else:
        scheduler = BackgroundScheduler()
        scheduler.add_job(
            lambda: run_batch(log_dir, args.parallel),
            "cron",
            hour=args.hour,
            minute=args.minute,
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        print(
            f"Scheduler started: cron daily at {args.hour:02d}:{args.minute:02d} (local). "
            f"Logs -> {log_dir}. Ctrl+C to stop.",
            flush=True,
        )

    try:
        if args.every_minute and not args.no_exit_after_window:
            while not stop_event.wait(timeout=1.0):
                pass
            scheduler.shutdown(wait=True)
            print(f"[{datetime.now().isoformat()}] Window finished; exiting.", flush=True)
        else:
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        scheduler.shutdown(wait=False)
        print("Stopped.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
