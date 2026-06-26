#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import statistics
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from fan_control_io import CpuLoadMeter, SysfsFan, read_int


@dataclass(frozen=True)
class Phase:
    name: str
    duration_s: int
    cpu_workers: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run fan-control acceptance test phases.")
    parser.add_argument("--phase", choices=["idle", "medium", "full"], required=True)
    parser.add_argument("--output-dir", default="/home/pi/fan-control/acceptance")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--idle-duration", type=int, default=600)
    parser.add_argument("--medium-duration", type=int, default=600)
    parser.add_argument("--full-duration", type=int, default=900)
    return parser.parse_args()


def phase_from_args(args: argparse.Namespace) -> Phase:
    if args.phase == "idle":
        return Phase("idle", args.idle_duration, 0)
    if args.phase == "medium":
        return Phase("medium", args.medium_duration, 2)
    return Phase("full", args.full_duration, 4)


def systemctl_props() -> dict[str, str]:
    result = subprocess.run(
        ["systemctl", "show", "fan-control.service", "-p", "MainPID", "-p", "NRestarts", "-p", "ActiveState", "-p", "SubState"],
        check=False,
        text=True,
        capture_output=True,
    )
    props: dict[str, str] = {}
    if result.returncode != 0:
        props["systemctl_error"] = result.stderr.strip()
        return props
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            props[key] = value
    return props


def summarize(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"min": None, "max": None, "mean": None, "stdev": None}
    return {
        "min": min(values),
        "max": max(values),
        "mean": statistics.fmean(values),
        "stdev": statistics.pstdev(values) if len(values) > 1 else 0.0,
    }


def journal_warnings_since(epoch: float) -> list[str]:
    result = subprocess.run(
        [
            "journalctl",
            "-u",
            "fan-control.service",
            "--since",
            f"@{int(epoch)}",
            "-p",
            "warning..alert",
            "--no-pager",
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        return [f"journalctl failed: {result.stderr.strip()}"]
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if len(lines) == 1 and "No entries" in lines[0]:
        return []
    return lines


def start_stress(phase: Phase) -> subprocess.Popen[str] | None:
    if phase.cpu_workers <= 0:
        return None
    return subprocess.Popen(
        ["stress-ng", "--cpu", str(phase.cpu_workers), "--timeout", str(phase.duration_s), "--metrics-brief"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def run_phase(phase: Phase, output_dir: Path, interval: float) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    fan = SysfsFan.discover()
    load_meter = CpuLoadMeter()
    started_wall = time.time()
    started_mono = time.monotonic()
    deadline = started_mono + phase.duration_s
    csv_path = output_dir / f"{phase.name}.csv"
    stress = start_stress(phase)
    rows: list[dict[str, object]] = []
    initial_props = systemctl_props()

    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "timestamp",
                "elapsed_s",
                "temp_c",
                "pwm",
                "rpm",
                "enable",
                "load",
                "freq_mhz",
                "active_state",
                "sub_state",
                "main_pid",
                "n_restarts",
            ],
        )
        writer.writeheader()
        while True:
            now = time.monotonic()
            elapsed = now - started_mono
            props = systemctl_props()
            row = {
                "timestamp": f"{time.time():.3f}",
                "elapsed_s": f"{elapsed:.1f}",
                "temp_c": fan.read_temp_c(),
                "pwm": fan.read_pwm(),
                "rpm": fan.read_rpm(),
                "enable": read_int(fan.enable_path),
                "load": load_meter.read(),
                "freq_mhz": fan.read_freq_mhz(),
                "active_state": props.get("ActiveState", ""),
                "sub_state": props.get("SubState", ""),
                "main_pid": props.get("MainPID", ""),
                "n_restarts": props.get("NRestarts", ""),
            }
            rows.append(row)
            writer.writerow(row)
            handle.flush()
            if now >= deadline:
                break
            time.sleep(min(interval, max(0.0, deadline - now)))

    stress_stdout = ""
    stress_stderr = ""
    stress_returncode = None
    if stress is not None:
        stress_stdout, stress_stderr = stress.communicate(timeout=30)
        stress_returncode = stress.returncode

    final_props = systemctl_props()
    temps = [float(row["temp_c"]) for row in rows]
    pwms = [float(row["pwm"]) for row in rows]
    rpms = [float(row["rpm"]) for row in rows]
    loads = [float(row["load"]) for row in rows]
    main_pids = {str(row["main_pid"]) for row in rows if str(row["main_pid"])}
    restart_values = [int(str(row["n_restarts"])) for row in rows if str(row["n_restarts"]).isdigit()]
    restart_delta = None
    if restart_values:
        restart_delta = max(restart_values) - min(restart_values)

    summary: dict[str, object] = {
        "phase": phase.name,
        "duration_s": phase.duration_s,
        "cpu_workers": phase.cpu_workers,
        "sample_count": len(rows),
        "csv_path": str(csv_path),
        "started_epoch": started_wall,
        "finished_epoch": time.time(),
        "fan_paths": {
            "temp": str(fan.temp_path),
            "pwm": str(fan.pwm_path),
            "enable": str(fan.enable_path),
            "rpm": str(fan.rpm_path),
            "freq": str(fan.freq_path),
        },
        "temperature_c": summarize(temps),
        "pwm": summarize(pwms),
        "rpm": summarize(rpms),
        "load": summarize(loads),
        "main_pids_seen": sorted(main_pids),
        "restart_delta": restart_delta,
        "initial_service": initial_props,
        "final_service": final_props,
        "journal_warnings": journal_warnings_since(started_wall),
        "stress_returncode": stress_returncode,
        "stress_stdout_tail": stress_stdout.splitlines()[-20:],
        "stress_stderr_tail": stress_stderr.splitlines()[-20:],
    }
    json_path = output_dir / f"{phase.name}.json"
    json_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def restore_sudo_owner(path: Path) -> None:
    uid = os.environ.get("SUDO_UID")
    gid = os.environ.get("SUDO_GID")
    if uid is None or gid is None:
        return
    for item in [path, *path.glob("*")]:
        try:
            os.chown(item, int(uid), int(gid))
        except PermissionError:
            pass


def main() -> int:
    args = parse_args()
    phase = phase_from_args(args)
    output_dir = Path(args.output_dir) / time.strftime("%Y%m%d-%H%M%S")
    if "ACCEPTANCE_RUN_DIR" in os.environ:
        output_dir = Path(os.environ["ACCEPTANCE_RUN_DIR"])
    summary = run_phase(phase, output_dir, args.interval)
    restore_sudo_owner(output_dir)
    print(json.dumps({key: value for key, value in summary.items() if key not in {"stress_stdout_tail", "stress_stderr_tail"}}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
