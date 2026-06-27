#!/usr/bin/env python3
from __future__ import annotations

import _pathfix  # noqa: F401  # adds ../src to sys.path; must precede src imports

import argparse
import json
import math
import multiprocessing as mp
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

from fan_control_io import SysfsFan


def burn_cpu(stop: mp.Event) -> None:
    value = 0.0
    while not stop.is_set():
        for index in range(50000):
            value += math.sqrt((index % 97) + 1)
        if value > 1e12:
            value = 0.0


def read_proc_stat(pid: int) -> dict[str, float]:
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
    ticks = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
    utime = int(stat[13]) / ticks
    stime = int(stat[14]) / ticks
    rss_pages = int(stat[23])
    rss_kb = rss_pages * os.sysconf("SC_PAGE_SIZE") / 1024.0
    return {"cpu_seconds": utime + stime, "rss_kb": rss_kb}


def restore_sudo_owner(path: Path) -> None:
    uid = os.environ.get("SUDO_UID")
    gid = os.environ.get("SUDO_GID")
    if uid is None or gid is None:
        return
    try:
        os.chown(path, int(uid), int(gid))
    except PermissionError:
        pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pressure-test and measure fan controller overhead.")
    parser.add_argument("--duration", type=float, default=90.0)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--workers", type=int, default=max(1, min(4, os.cpu_count() or 1)))
    parser.add_argument("--controller", default="/home/pi/fan-control/src/fan_control.py")
    parser.add_argument("--output-dir", default="/home/pi/fan-control/data")
    parser.add_argument("--dry-run", action="store_true", help="Do not write PWM during evaluation")
    parser.add_argument("--control-mode", choices=("zone-mpc",), default="zone-mpc")
    parser.add_argument("--zone-low", type=float, default=50.0)
    parser.add_argument("--zone-high", type=float, default=60.0)
    parser.add_argument("--full-temp", type=float, default=69.0)
    parser.add_argument("--mpc-horizon", type=int, default=12)
    parser.add_argument("--mpc-candidate-step", type=int, default=5)
    parser.add_argument("--max-step", type=int, default=20)
    parser.add_argument("--min-active-pwm", type=int, default=75)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir.chmod(0o755)
    restore_sudo_owner(output_dir)
    output = output_dir / f"evaluation-{int(time.time())}.json"
    fan = SysfsFan.discover()
    stop = mp.Event()
    workers = [mp.Process(target=burn_cpu, args=(stop,)) for _ in range(args.workers)]
    controller_cmd = [
        sys.executable,
        args.controller,
        "--duration",
        str(args.duration),
        "--interval",
        str(args.interval),
        "--control-mode",
        args.control_mode,
        "--zone-low",
        str(args.zone_low),
        "--zone-high",
        str(args.zone_high),
        "--full-temp",
        str(args.full_temp),
        "--mpc-horizon",
        str(args.mpc_horizon),
        "--mpc-candidate-step",
        str(args.mpc_candidate_step),
        "--max-step",
        str(args.max_step),
        "--min-active-pwm",
        str(args.min_active_pwm),
    ]
    if args.dry_run:
        controller_cmd.append("--dry-run")

    for worker in workers:
        worker.start()

    started = time.monotonic()
    proc = subprocess.Popen(
        controller_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    samples: list[dict[str, float]] = []
    controller_stats: list[dict[str, float]] = []
    try:
        while proc.poll() is None:
            now = time.monotonic() - started
            try:
                samples.append(
                    {
                        "elapsed_s": now,
                        "temp_c": fan.read_temp_c(),
                        "pwm": fan.read_pwm(),
                        "rpm": fan.read_rpm(),
                    }
                )
                controller_stats.append({"elapsed_s": now, **read_proc_stat(proc.pid)})
            except FileNotFoundError:
                break
            time.sleep(max(0.5, min(args.interval, 2.0)))
    finally:
        stop.set()
        for worker in workers:
            worker.join(timeout=3)
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
        stdout, stderr = proc.communicate(timeout=10)

    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    elapsed = max(time.monotonic() - started, 0.001)
    cpu_seconds = 0.0
    max_rss_kb = 0.0
    if controller_stats:
        cpu_seconds = max(item["cpu_seconds"] for item in controller_stats)
        max_rss_kb = max(item["rss_kb"] for item in controller_stats)

    result = {
        "duration_s": elapsed,
        "workers": args.workers,
        "controller_command": controller_cmd,
        "controller_returncode": proc.returncode,
        "controller_cpu_seconds": cpu_seconds,
        "controller_cpu_percent_of_one_core": 100.0 * cpu_seconds / elapsed,
        "controller_max_rss_kb": max_rss_kb,
        "children_cpu_seconds_total": usage.ru_utime + usage.ru_stime,
        "temperature_min_c": min((item["temp_c"] for item in samples), default=None),
        "temperature_max_c": max((item["temp_c"] for item in samples), default=None),
        "temperature_final_c": samples[-1]["temp_c"] if samples else None,
        "samples": samples,
        "controller_stdout_tail": stdout.splitlines()[-20:],
        "controller_stderr_tail": stderr.splitlines()[-20:],
    }
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    output.chmod(0o644)
    restore_sudo_owner(output)

    print(json.dumps({key: result[key] for key in result if key != "samples"}, indent=2))
    print(f"Wrote evaluation to {output}")
    return 0 if proc.returncode == 0 else proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
