#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fan_control_io import CpuLoadMeter, SysfsFan, ensure_root_for_writes
from model_identification import build_identification_schedule


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a dedicated Raspberry Pi fan thermal identification experiment.")
    parser.add_argument("--output-dir", default="/home/pi/fan-control/data/identification")
    parser.add_argument("--phase-duration", type=int, default=180)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--safety-temp", type=float, default=70.0)
    parser.add_argument("--abort-temp", type=float, default=75.0)
    parser.add_argument("--service", default="fan-control.service")
    parser.add_argument("--no-service-control", action="store_true")
    return parser.parse_args()


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


def run_systemctl(action: str, service: str) -> dict[str, Any]:
    result = subprocess.run(["systemctl", action, service], text=True, capture_output=True, check=False)
    return {"action": action, "returncode": result.returncode, "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}


def start_load(workers: int, duration_s: int) -> list[subprocess.Popen[str]]:
    if workers <= 0:
        return []
    stress_ng = shutil.which("stress-ng")
    if stress_ng is not None:
        return [
            subprocess.Popen(
                [stress_ng, "--cpu", str(workers), "--timeout", str(duration_s), "--metrics-brief"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        ]
    burner = (
        "import math,sys,time\n"
        "end=time.monotonic()+float(sys.argv[1])\n"
        "value=0.0\n"
        "while time.monotonic()<end:\n"
        "    for index in range(50000):\n"
        "        value += math.sqrt((index % 97) + 1)\n"
        "    if value > 1e12:\n"
        "        value = 0.0\n"
    )
    return [subprocess.Popen([sys.executable, "-c", burner, str(duration_s)], text=True) for _ in range(workers)]


def finish_load(processes: list[subprocess.Popen[str]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for process in processes:
        if process.poll() is None:
            process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
        results.append(
            {
                "returncode": process.returncode,
                "stdout_tail": (stdout or "").splitlines()[-5:],
                "stderr_tail": (stderr or "").splitlines()[-5:],
            }
        )
    return results


def write_pwm_for_temp(fan: SysfsFan, scheduled_pwm: int, temp_c: float, safety_temp_c: float) -> tuple[int, bool]:
    if temp_c >= safety_temp_c:
        fan.write_pwm(255)
        return 255, True
    fan.write_pwm(scheduled_pwm)
    return scheduled_pwm, False


def run_experiment(args: argparse.Namespace) -> dict[str, Any]:
    ensure_root_for_writes()
    run_dir = Path(args.output_dir) / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    run_dir.chmod(0o755)
    csv_path = run_dir / "samples.csv"
    schedule = build_identification_schedule(args.phase_duration)
    service_events: list[dict[str, Any]] = []
    load_results: list[dict[str, Any]] = []
    stopped = False

    fan = SysfsFan.discover()
    load_meter = CpuLoadMeter()
    started_epoch = time.time()
    started_mono = time.monotonic()
    stop_requested = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    try:
        if not args.no_service_control:
            service_events.append(run_systemctl("stop", args.service))
            stopped = True
        fan.set_manual()
        fieldnames = [
            "timestamp",
            "elapsed_s",
            "phase_index",
            "load_name",
            "cpu_workers",
            "scheduled_pwm",
            "command_pwm",
            "safety_override",
            "temp_c",
            "pwm",
            "rpm",
            "load",
            "freq_mhz",
        ]
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for phase in schedule:
                if stop_requested:
                    break
                processes = start_load(phase.cpu_workers, phase.duration_s)
                phase_deadline = time.monotonic() + phase.duration_s
                try:
                    while not stop_requested:
                        now = time.monotonic()
                        temp_c = fan.read_temp_c()
                        if temp_c >= args.abort_temp:
                            fan.write_pwm(255)
                            raise RuntimeError(f"abort temperature reached: {temp_c:.2f}C")
                        command_pwm, safety_override = write_pwm_for_temp(
                            fan=fan,
                            scheduled_pwm=phase.scheduled_pwm,
                            temp_c=temp_c,
                            safety_temp_c=args.safety_temp,
                        )
                        writer.writerow(
                            {
                                "timestamp": f"{time.time():.3f}",
                                "elapsed_s": f"{now - started_mono:.3f}",
                                "phase_index": phase.index,
                                "load_name": phase.load_name,
                                "cpu_workers": phase.cpu_workers,
                                "scheduled_pwm": phase.scheduled_pwm,
                                "command_pwm": command_pwm,
                                "safety_override": int(safety_override),
                                "temp_c": f"{temp_c:.3f}",
                                "pwm": fan.read_pwm(),
                                "rpm": fan.read_rpm(),
                                "load": f"{load_meter.read():.5f}",
                                "freq_mhz": f"{fan.read_freq_mhz():.1f}",
                            }
                        )
                        handle.flush()
                        if now >= phase_deadline:
                            break
                        time.sleep(min(args.interval, max(0.0, phase_deadline - now)))
                finally:
                    load_results.extend(finish_load(processes))
        finished_epoch = time.time()
        summary = {
            "run_dir": str(run_dir),
            "csv_path": str(csv_path),
            "started_epoch": started_epoch,
            "finished_epoch": finished_epoch,
            "elapsed_s": finished_epoch - started_epoch,
            "phase_duration_s": args.phase_duration,
            "interval_s": args.interval,
            "safety_temp_c": args.safety_temp,
            "abort_temp_c": args.abort_temp,
            "schedule": [asdict(phase) for phase in schedule],
            "fan_paths": {
                "temp": str(fan.temp_path),
                "pwm": str(fan.pwm_path),
                "enable": str(fan.enable_path),
                "rpm": str(fan.rpm_path),
                "freq": str(fan.freq_path),
            },
            "service_events": service_events,
            "load_results_tail": load_results[-10:],
        }
        (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        return summary
    finally:
        try:
            fan.write_pwm(255 if fan.read_temp_c() >= args.safety_temp else 75)
        except Exception:
            pass
        if stopped and not args.no_service_control:
            service_events.append(run_systemctl("start", args.service))
        restore_sudo_owner(run_dir)


def main() -> int:
    args = parse_args()
    summary = run_experiment(args)
    print(json.dumps({key: summary[key] for key in summary if key != "schedule"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
