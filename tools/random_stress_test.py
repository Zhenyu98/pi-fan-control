#!/usr/bin/env python3
from __future__ import annotations

import _pathfix  # noqa: F401  # adds ../src to sys.path; must precede src imports

import argparse
import csv
import json
import math
import os
import random
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from fan_control_core import ThermalPredictor, load_predictor
from fan_control_io import CpuLoadMeter, SysfsFan


@dataclass(frozen=True)
class StressPhase:
    index: int
    duration_s: int
    cpu_workers: int


def build_random_schedule(
    total_duration_s: int,
    min_phase_s: int,
    max_phase_s: int,
    max_workers: int,
    seed: int,
) -> list[StressPhase]:
    if total_duration_s <= 0:
        raise ValueError("total_duration_s must be positive")
    if min_phase_s <= 0 or max_phase_s < min_phase_s:
        raise ValueError("phase durations are invalid")
    if max_workers < 0:
        raise ValueError("max_workers must not be negative")

    rng = random.Random(seed)
    phases: list[StressPhase] = []
    remaining = total_duration_s
    index = 1
    while remaining > 0:
        duration = min(remaining, rng.randint(min_phase_s, max_phase_s))
        phases.append(StressPhase(index=index, duration_s=duration, cpu_workers=rng.randint(0, max_workers)))
        remaining -= duration
        index += 1
    return phases


def prediction_summary(samples: list[dict[str, Any]], model: ThermalPredictor) -> dict[str, float | int | None]:
    errors: list[float] = []
    direction_matches = 0
    direction_count = 0

    for index, (current, following) in enumerate(zip(samples, samples[1:])):
        previous = samples[index - 1] if index > 0 else current
        current_temp = float(current["temp_c"])
        actual_next = float(following["temp_c"])
        predicted_next = model.predict_with_state(
            temp_c=current_temp,
            pwm=float(current["pwm"]),
            load=float(current["load"]),
            prev_temp_c=float(previous["temp_c"]),
            prev_pwm=float(previous["pwm"]),
            prev_load=float(previous["load"]),
        )
        error = predicted_next - actual_next
        errors.append(error)

        actual_delta = actual_next - current_temp
        predicted_delta = predicted_next - current_temp
        if abs(actual_delta) >= 0.25 or abs(predicted_delta) >= 0.25:
            direction_count += 1
            if (actual_delta >= 0 and predicted_delta >= 0) or (actual_delta < 0 and predicted_delta < 0):
                direction_matches += 1

    if not errors:
        return {
            "paired_samples": 0,
            "mae_c": None,
            "rmse_c": None,
            "bias_c": None,
            "max_abs_error_c": None,
            "direction_accuracy": None,
            "direction_samples": 0,
        }

    absolute_errors = [abs(error) for error in errors]
    return {
        "paired_samples": len(errors),
        "mae_c": statistics.fmean(absolute_errors),
        "rmse_c": math.sqrt(statistics.fmean([error * error for error in errors])),
        "bias_c": statistics.fmean(errors),
        "max_abs_error_c": max(absolute_errors),
        "direction_accuracy": direction_matches / direction_count if direction_count else None,
        "direction_samples": direction_count,
    }


def summarize(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"min": None, "max": None, "mean": None, "stdev": None}
    return {
        "min": min(values),
        "max": max(values),
        "mean": statistics.fmean(values),
        "stdev": statistics.pstdev(values) if len(values) > 1 else 0.0,
    }


def load_model(path: str | Path) -> tuple[ThermalPredictor, str]:
    model_path = Path(path)
    if model_path.exists():
        return load_predictor(model_path), str(model_path)
    from fan_control_core import ThermalModel

    return ThermalModel.default(), "default"


def systemctl_props() -> dict[str, str]:
    result = subprocess.run(
        [
            "systemctl",
            "show",
            "fan-control.service",
            "-p",
            "MainPID",
            "-p",
            "NRestarts",
            "-p",
            "ActiveState",
            "-p",
            "SubState",
        ],
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


def read_proc_stat(pid: str | int) -> dict[str, float | None]:
    try:
        fields = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8").split()
    except (FileNotFoundError, ValueError):
        return {"cpu_seconds": None, "rss_kb": None}

    ticks = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
    utime = int(fields[13]) / ticks
    stime = int(fields[14]) / ticks
    rss_pages = int(fields[23])
    rss_kb = rss_pages * os.sysconf("SC_PAGE_SIZE") / 1024.0
    return {"cpu_seconds": utime + stime, "rss_kb": rss_kb}


def start_load(phase: StressPhase) -> list[subprocess.Popen[str]]:
    if phase.cpu_workers <= 0:
        return []

    stress_ng = shutil.which("stress-ng")
    if stress_ng is not None:
        return [
            subprocess.Popen(
                [
                    stress_ng,
                    "--cpu",
                    str(phase.cpu_workers),
                    "--timeout",
                    str(phase.duration_s),
                    "--metrics-brief",
                ],
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
    return [
        subprocess.Popen([sys.executable, "-c", burner, str(phase.duration_s)], text=True)
        for _ in range(phase.cpu_workers)
    ]


def finish_load(processes: list[subprocess.Popen[str]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for process in processes:
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            stdout, stderr = process.communicate(timeout=5)
        results.append(
            {
                "returncode": process.returncode,
                "stdout_tail": (stdout or "").splitlines()[-5:],
                "stderr_tail": (stderr or "").splitlines()[-5:],
            }
        )
    return results


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run randomized CPU pressure against the live fan-control service.")
    parser.add_argument("--duration", type=int, default=480)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--min-phase", type=int, default=20)
    parser.add_argument("--max-phase", type=int, default=75)
    parser.add_argument("--max-workers", type=int, default=max(1, min(4, os.cpu_count() or 1)))
    parser.add_argument("--seed", type=int, default=int(time.time()))
    parser.add_argument("--model", default="/home/pi/fan-control/data/model_arx2_m2.json")
    parser.add_argument("--output-dir", default="/home/pi/fan-control/acceptance")
    parser.add_argument("--zone-high", type=float, default=60.0)
    parser.add_argument("--full-temp", type=float, default=69.0)
    parser.add_argument("--safety-temp", type=float, default=70.0)
    return parser.parse_args()


def run(args: argparse.Namespace) -> dict[str, Any]:
    run_name = f"random-stress-{time.strftime('%Y%m%d-%H%M%S')}"
    run_dir = Path(args.output_dir) / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    run_dir.chmod(0o755)

    fan = SysfsFan.discover()
    load_meter = CpuLoadMeter()
    model, model_source = load_model(args.model)
    schedule = build_random_schedule(
        total_duration_s=args.duration,
        min_phase_s=args.min_phase,
        max_phase_s=args.max_phase,
        max_workers=args.max_workers,
        seed=args.seed,
    )

    csv_path = run_dir / "samples.csv"
    started_epoch = time.time()
    started_mono = time.monotonic()
    rows: list[dict[str, Any]] = []
    load_results: list[dict[str, Any]] = []
    initial_props = systemctl_props()

    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "timestamp",
            "elapsed_s",
            "phase_index",
            "phase_workers",
            "temp_c",
            "pwm",
            "rpm",
            "load",
            "freq_mhz",
            "active_state",
            "sub_state",
            "main_pid",
            "n_restarts",
            "controller_cpu_seconds",
            "controller_rss_kb",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()

        for phase in schedule:
            phase_deadline = time.monotonic() + phase.duration_s
            processes = start_load(phase)
            try:
                while True:
                    now = time.monotonic()
                    elapsed = now - started_mono
                    props = systemctl_props()
                    proc_stats = read_proc_stat(props.get("MainPID", "0"))
                    row = {
                        "timestamp": f"{time.time():.3f}",
                        "elapsed_s": elapsed,
                        "phase_index": phase.index,
                        "phase_workers": phase.cpu_workers,
                        "temp_c": fan.read_temp_c(),
                        "pwm": fan.read_pwm(),
                        "rpm": fan.read_rpm(),
                        "load": load_meter.read(),
                        "freq_mhz": fan.read_freq_mhz(),
                        "active_state": props.get("ActiveState", ""),
                        "sub_state": props.get("SubState", ""),
                        "main_pid": props.get("MainPID", ""),
                        "n_restarts": props.get("NRestarts", ""),
                        "controller_cpu_seconds": proc_stats["cpu_seconds"],
                        "controller_rss_kb": proc_stats["rss_kb"],
                    }
                    rows.append(row)
                    writer.writerow(row)
                    handle.flush()
                    if now >= phase_deadline:
                        break
                    time.sleep(min(args.interval, max(0.0, phase_deadline - now)))
            finally:
                load_results.extend(finish_load(processes))

    finished_epoch = time.time()
    elapsed_s = max(time.monotonic() - started_mono, 0.001)
    restore_sudo_owner(run_dir)

    temps = [float(row["temp_c"]) for row in rows]
    pwms = [float(row["pwm"]) for row in rows]
    rpms = [float(row["rpm"]) for row in rows]
    loads = [float(row["load"]) for row in rows]
    cpu_values = [float(row["controller_cpu_seconds"]) for row in rows if row["controller_cpu_seconds"] is not None]
    rss_values = [float(row["controller_rss_kb"]) for row in rows if row["controller_rss_kb"] is not None]
    restart_values = [int(str(row["n_restarts"])) for row in rows if str(row["n_restarts"]).isdigit()]
    over_zone = [temp for temp in temps if temp > args.zone_high]

    controller_cpu_seconds = max(cpu_values) - min(cpu_values) if len(cpu_values) >= 2 else None
    controller_cpu_percent = (
        100.0 * controller_cpu_seconds / elapsed_s if controller_cpu_seconds is not None else None
    )
    summary: dict[str, Any] = {
        "run_dir": str(run_dir),
        "csv_path": str(csv_path),
        "started_epoch": started_epoch,
        "finished_epoch": finished_epoch,
        "elapsed_s": elapsed_s,
        "seed": args.seed,
        "schedule": [asdict(phase) for phase in schedule],
        "model_source": model_source,
        "model": model.to_dict() if hasattr(model, "to_dict") else asdict(model),
        "fan_paths": {
            "temp": str(fan.temp_path),
            "pwm": str(fan.pwm_path),
            "rpm": str(fan.rpm_path),
            "freq": str(fan.freq_path),
        },
        "temperature_c": summarize(temps),
        "pwm": summarize(pwms),
        "rpm": summarize(rpms),
        "load": summarize(loads),
        "prediction": prediction_summary(rows, model),
        "zone": {
            "zone_high_c": args.zone_high,
            "full_temp_c": args.full_temp,
            "safety_temp_c": args.safety_temp,
            "samples_over_zone_high": len(over_zone),
            "percent_over_zone_high": 100.0 * len(over_zone) / len(temps) if temps else None,
            "max_overshoot_c": max((temp - args.zone_high for temp in temps), default=None),
            "samples_at_or_above_full_temp": len([temp for temp in temps if temp >= args.full_temp]),
            "samples_at_or_above_safety_temp": len([temp for temp in temps if temp >= args.safety_temp]),
        },
        "controller_overhead": {
            "cpu_seconds": controller_cpu_seconds,
            "cpu_percent_of_one_core": controller_cpu_percent,
            "max_rss_kb": max(rss_values) if rss_values else None,
            "main_pids_seen": sorted({str(row["main_pid"]) for row in rows if str(row["main_pid"])}),
            "restart_delta": max(restart_values) - min(restart_values) if restart_values else None,
        },
        "initial_service": initial_props,
        "final_service": systemctl_props(),
        "journal_warnings": journal_warnings_since(started_epoch),
        "load_results_tail": load_results[-10:],
    }

    summary_path = run_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    report_path = run_dir / "report.md"
    report_path.write_text(render_report(summary), encoding="utf-8")
    restore_sudo_owner(run_dir)
    return summary


def format_optional(value: Any, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_report(summary: dict[str, Any]) -> str:
    prediction = summary["prediction"]
    overhead = summary["controller_overhead"]
    zone = summary["zone"]
    temperature = summary["temperature_c"]
    pwm = summary["pwm"]
    rpm = summary["rpm"]
    load = summary["load"]

    return (
        "# Random Stress Evaluation\n\n"
        f"Run directory: `{summary['run_dir']}`\n\n"
        "## Summary\n\n"
        f"- Seed: `{summary['seed']}`\n"
        f"- Elapsed: `{format_optional(summary['elapsed_s'])}s`\n"
        f"- Temperature min/mean/max: `{format_optional(temperature['min'])}` / "
        f"`{format_optional(temperature['mean'])}` / `{format_optional(temperature['max'])}` C\n"
        f"- PWM min/mean/max: `{format_optional(pwm['min'])}` / `{format_optional(pwm['mean'])}` / "
        f"`{format_optional(pwm['max'])}`\n"
        f"- RPM min/mean/max: `{format_optional(rpm['min'])}` / `{format_optional(rpm['mean'])}` / "
        f"`{format_optional(rpm['max'])}`\n"
        f"- Load min/mean/max: `{format_optional(load['min'])}` / `{format_optional(load['mean'])}` / "
        f"`{format_optional(load['max'])}`\n\n"
        "## Prediction\n\n"
        f"- Model source: `{summary['model_source']}`\n"
        f"- One-step paired samples: `{prediction['paired_samples']}`\n"
        f"- MAE: `{format_optional(prediction['mae_c'])} C`\n"
        f"- RMSE: `{format_optional(prediction['rmse_c'])} C`\n"
        f"- Bias: `{format_optional(prediction['bias_c'])} C`\n"
        f"- Direction accuracy: `{format_optional(prediction['direction_accuracy'], 3)}` "
        f"over `{prediction['direction_samples']}` significant samples\n\n"
        "## Zone Behavior\n\n"
        f"- Samples over zone high `{zone['zone_high_c']} C`: `{zone['samples_over_zone_high']}` "
        f"(`{format_optional(zone['percent_over_zone_high'])}%`)\n"
        f"- Max overshoot above zone high: `{format_optional(zone['max_overshoot_c'])} C`\n"
        f"- Samples at or above full speed temp `{zone['full_temp_c']} C`: "
        f"`{zone['samples_at_or_above_full_temp']}`\n"
        f"- Samples at or above safety temp `{zone['safety_temp_c']} C`: "
        f"`{zone['samples_at_or_above_safety_temp']}`\n\n"
        "## Controller Overhead\n\n"
        f"- CPU seconds: `{format_optional(overhead['cpu_seconds'], 4)}`\n"
        f"- CPU percent of one core: `{format_optional(overhead['cpu_percent_of_one_core'], 3)}%`\n"
        f"- Max RSS: `{format_optional(overhead['max_rss_kb'])} KiB`\n"
        f"- Main PIDs seen: `{', '.join(overhead['main_pids_seen'])}`\n"
        f"- Restart delta: `{overhead['restart_delta']}`\n\n"
        "## Service Health\n\n"
        f"- Final service: `{summary['final_service']}`\n"
        f"- Journal warnings: `{len(summary['journal_warnings'])}`\n"
    )


def main() -> int:
    args = parse_args()
    summary = run(args)
    print(json.dumps({key: summary[key] for key in summary if key not in {"schedule"}}, indent=2))
    print(f"Wrote report to {summary['run_dir']}/report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
