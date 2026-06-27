#!/usr/bin/env python3
from __future__ import annotations

import _pathfix  # noqa: F401  # adds ../src to sys.path; must precede src imports

import argparse
import csv
import json
import math
import os
import statistics
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fan_control_core import PredictionObserver, ZoneMpcController, load_predictor
from fan_control_io import CpuLoadMeter, SysfsFan, ensure_root_for_writes
from random_stress_test import build_random_schedule, finish_load, restore_sudo_owner, start_load


OFFICIAL_FAN_STEPS = [
    {"temp_c": 40.0, "hyst_c": 2.0, "pwm": 128},
    {"temp_c": 50.0, "hyst_c": 5.0, "pwm": 192},
    {"temp_c": 60.0, "hyst_c": 5.0, "pwm": 225},
    {"temp_c": 65.0, "hyst_c": 5.0, "pwm": 255},
]


def official_step_pwm(temp_c: float, previous_pwm: int, pwm_scale: float = 1.0) -> int:
    def scaled_pwm(raw_pwm: float) -> int:
        return int(round(max(0.0, min(255.0, raw_pwm * pwm_scale))))

    target_pwm = 0
    for step in OFFICIAL_FAN_STEPS:
        if temp_c >= step["temp_c"]:
            target_pwm = scaled_pwm(step["pwm"])

    if target_pwm >= previous_pwm:
        return target_pwm

    held_pwm = target_pwm
    for step in OFFICIAL_FAN_STEPS:
        step_pwm = scaled_pwm(step["pwm"])
        release_temp = float(step["temp_c"]) - float(step["hyst_c"])
        if previous_pwm >= step_pwm and temp_c >= release_temp:
            held_pwm = max(held_pwm, step_pwm)
    return held_pwm


def count_pwm_changes(pwms: list[int | float], threshold: float = 0.0) -> int:
    return sum(1 for previous, current in zip(pwms, pwms[1:]) if abs(float(current) - float(previous)) > threshold)


def count_pwm_reversals(pwms: list[int | float], threshold: float = 0.0) -> int:
    directions: list[int] = []
    for previous, current in zip(pwms, pwms[1:]):
        delta = float(current) - float(previous)
        if abs(delta) <= threshold:
            continue
        directions.append(1 if delta > 0 else -1)
    return sum(1 for previous, current in zip(directions, directions[1:]) if previous != current)


def phase_load_mean(samples: list[dict[str, Any]]) -> dict[str, float]:
    by_phase: dict[str, list[float]] = {}
    for sample in samples:
        phase = str(sample.get("phase_index", ""))
        if not phase:
            continue
        by_phase.setdefault(phase, []).append(float(sample.get("load", 0.0)))
    return {phase: statistics.fmean(values) for phase, values in sorted(by_phase.items()) if values}


def compare_arm_fairness(constant: dict[str, Any], segmented: dict[str, Any]) -> dict[str, Any]:
    constant_phase_load = constant.get("phase_load_mean", {})
    segmented_phase_load = segmented.get("phase_load_mean", {})
    common_phases = sorted(set(constant_phase_load) & set(segmented_phase_load))
    phase_load_errors = [
        float(constant_phase_load[phase]) - float(segmented_phase_load[phase]) for phase in common_phases
    ]
    phase_load_rmse = (
        math.sqrt(statistics.fmean([error * error for error in phase_load_errors])) if phase_load_errors else None
    )
    return {
        "start_temp_delta_abs_c": abs(float(constant["temp_start_c"]) - float(segmented["temp_start_c"])),
        "load_mean_delta_abs": abs(float(constant["load_mean"]) - float(segmented["load_mean"])),
        "phase_load_rmse": phase_load_rmse,
        "common_phase_count": len(common_phases),
    }


def choose_precondition_action(temp_c: float, target_c: float, tolerance_c: float, mode: str) -> str:
    if temp_c > target_c + tolerance_c:
        return "cool"
    if temp_c >= target_c - tolerance_c:
        return "hold"
    if mode == "warm-cool":
        return "warm"
    if mode == "cool-only":
        return "wait"
    raise ValueError("precondition mode must be 'cool-only' or 'warm-cool'")


def summarize_samples(samples: list[dict[str, Any]], zone_high_c: float) -> dict[str, Any]:
    if not samples:
        return {
            "sample_count": 0,
            "duration_s": 0.0,
            "temp_peak_c": None,
            "temp_mean_c": None,
            "over_zone_seconds": 0.0,
            "over_zone_percent": 0.0,
            "pwm_mean": None,
            "pwm_change_count": 0,
            "pwm_reversal_count": 0,
            "rpm_mean": None,
            "rpm_peak": None,
            "load_mean": None,
            "load_peak": None,
            "phase_load_mean": {},
            "predicted_max_peak_c": None,
            "predicted_max_mean_c": None,
            "terminal_peak_c": None,
            "terminal_mean_c": None,
        }

    elapsed = [float(sample["elapsed_s"]) for sample in samples]
    temps = [float(sample["temp_c"]) for sample in samples]
    loads = [float(sample.get("load", 0.0)) for sample in samples]
    pwms = [int(float(sample["pwm"])) for sample in samples]
    rpms = [int(float(sample["rpm"])) for sample in samples]
    predicted_max_values = [float(sample["predicted_max_temp_c"]) for sample in samples]
    terminal_values = [float(sample["terminal_temp_c"]) for sample in samples]
    deltas = [max(0.0, current - previous) for previous, current in zip(elapsed, elapsed[1:])]
    intervals = deltas
    duration_s = sum(intervals)
    over_zone_seconds = sum(
        interval for sample, interval in zip(samples[:-1], intervals) if float(sample["temp_c"]) > zone_high_c
    )

    return {
        "sample_count": len(samples),
        "duration_s": duration_s,
        "temp_start_c": temps[0],
        "temp_end_c": temps[-1],
        "temp_peak_c": max(temps),
        "temp_mean_c": statistics.fmean(temps),
        "over_zone_seconds": over_zone_seconds,
        "over_zone_percent": 100.0 * over_zone_seconds / duration_s if duration_s > 0 else 0.0,
        "pwm_start": pwms[0],
        "pwm_end": pwms[-1],
        "pwm_mean": statistics.fmean(pwms),
        "pwm_peak": max(pwms),
        "pwm_change_count": count_pwm_changes(pwms),
        "pwm_reversal_count": count_pwm_reversals(pwms),
        "rpm_mean": statistics.fmean(rpms),
        "rpm_peak": max(rpms),
        "load_mean": statistics.fmean(loads),
        "load_peak": max(loads),
        "phase_load_mean": phase_load_mean(samples),
        "predicted_max_peak_c": max(predicted_max_values),
        "predicted_max_mean_c": statistics.fmean(predicted_max_values),
        "terminal_peak_c": max(terminal_values),
        "terminal_mean_c": statistics.fmean(terminal_values),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run real constant-vs-segmented Zone MPC AB test.")
    parser.add_argument("--duration", type=int, default=600)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--min-phase", type=int, default=20)
    parser.add_argument("--max-phase", type=int, default=60)
    parser.add_argument("--max-workers", type=int, default=max(1, min(4, os.cpu_count() or 1)))
    parser.add_argument("--seed", type=int, default=20260626)
    parser.add_argument("--modes", default="constant,segmented")
    parser.add_argument("--official-pwm-scale", type=float, default=1.0)
    parser.add_argument("--model", default="/home/pi/fan-control/data/model_arx2_m2.json")
    parser.add_argument("--output-dir", default="/home/pi/fan-control/acceptance")
    parser.add_argument("--zone-low", type=float, default=53.0)
    parser.add_argument("--zone-high", type=float, default=58.0)
    parser.add_argument("--full-temp", type=float, default=69.0)
    parser.add_argument("--safety-temp", type=float, default=70.0)
    parser.add_argument("--abort-temp", type=float, default=72.0)
    parser.add_argument("--horizon", type=int, default=12)
    parser.add_argument("--candidate-step", type=int, default=5)
    parser.add_argument("--segments", type=int, default=3)
    parser.add_argument("--segment-candidate-step", type=int, default=20)
    parser.add_argument("--max-step", type=int, default=20)
    parser.add_argument("--min-active-pwm", type=int, default=75)
    parser.add_argument("--idle-stop", type=float, default=50.0)
    parser.add_argument("--start-temp", type=float, default=57.0)
    parser.add_argument("--start-temp-tolerance", type=float, default=0.3)
    parser.add_argument("--start-hold-s", type=float, default=20.0)
    parser.add_argument("--start-stability-c", type=float, default=0.3)
    parser.add_argument("--start-settle-s", type=float, default=8.0)
    parser.add_argument("--precondition-timeout", type=int, default=420)
    parser.add_argument("--start-pwm", type=int, default=95)
    parser.add_argument("--cool-pwm", type=int, default=255)
    parser.add_argument("--warm-pwm", type=int, default=75)
    parser.add_argument("--precondition-mode", choices=("cool-only", "warm-cool"), default="cool-only")
    parser.add_argument("--allow-precondition-timeout", action="store_true")
    parser.add_argument("--service", default="fan-control.service")
    return parser.parse_args()


def service_is_active(service: str) -> bool:
    return subprocess.run(["systemctl", "is-active", "--quiet", service], check=False).returncode == 0


def systemctl(action: str, service: str) -> None:
    result = subprocess.run(["systemctl", action, service], check=False, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"systemctl {action} {service} failed: {result.stderr.strip()}")


def build_controller(args: argparse.Namespace, plan_mode: str) -> ZoneMpcController:
    model = load_predictor(args.model)
    return ZoneMpcController(
        model=model,
        zone_low_temp_c=args.zone_low,
        zone_high_temp_c=args.zone_high,
        full_temp_c=args.full_temp,
        max_pwm_step=args.max_step,
        min_active_pwm=args.min_active_pwm,
        idle_stop_temp_c=args.idle_stop,
        safety_temp_c=args.safety_temp,
        horizon_steps=args.horizon,
        candidate_pwm_step=args.candidate_step,
        plan_mode=plan_mode,
        segments=args.segments,
        segment_candidate_step=args.segment_candidate_step,
    )


def parse_modes(raw_modes: str) -> list[str]:
    modes = [mode.strip() for mode in raw_modes.split(",") if mode.strip()]
    allowed = {"constant", "segmented", "official-step"}
    if len(modes) != 2:
        raise ValueError("--modes must contain exactly two comma-separated modes")
    invalid = [mode for mode in modes if mode not in allowed]
    if invalid:
        raise ValueError(f"unsupported mode(s): {', '.join(invalid)}")
    return modes


def precondition_start_temp(
    fan: SysfsFan,
    target_c: float,
    tolerance_c: float,
    timeout_s: int,
    start_pwm: int,
    hold_s: float,
    stability_c: float,
    settle_s: float,
    cool_pwm: int,
    warm_pwm: int,
    mode: str,
) -> dict[str, Any]:
    started = time.monotonic()
    warm_processes: list[subprocess.Popen[str]] = []
    window_samples: list[tuple[float, float]] = []

    def stop_warm_processes() -> None:
        nonlocal warm_processes
        for process in warm_processes:
            if process.poll() is None:
                process.terminate()
        finish_load(warm_processes)
        warm_processes = []

    try:
        while True:
            temp_c = fan.read_temp_c()
            elapsed = time.monotonic() - started
            low = target_c - tolerance_c
            high = target_c + tolerance_c
            action = choose_precondition_action(temp_c, target_c, tolerance_c, mode)
            if action == "hold":
                stop_warm_processes()
                fan.write_pwm(start_pwm)
                now = time.monotonic()
                window_samples.append((now, temp_c))
                window_samples = [
                    (sample_time, sample_temp)
                    for sample_time, sample_temp in window_samples
                    if now - sample_time <= hold_s + 2.5
                ]
                window_temps = [sample_temp for _, sample_temp in window_samples]
                window_age = window_samples[-1][0] - window_samples[0][0] if len(window_samples) >= 2 else 0.0
                if (
                    window_age >= hold_s
                    and window_temps
                    and max(window_temps) - min(window_temps) <= stability_c
                ):
                    time.sleep(settle_s)
                    final_temp = fan.read_temp_c()
                    if low <= final_temp <= high:
                        return {
                            "temp_c": final_temp,
                            "elapsed_s": time.monotonic() - started,
                            "timed_out": False,
                            "hold_s": hold_s,
                            "stability_c": stability_c,
                            "window_min_c": min(window_temps),
                            "window_max_c": max(window_temps),
                            "window_sample_count": len(window_samples),
                        }
                    window_samples = []
            if elapsed >= timeout_s:
                fan.write_pwm(start_pwm)
                time.sleep(settle_s)
                final_temp = fan.read_temp_c()
                window_temps = [sample_temp for _, sample_temp in window_samples]
                return {
                    "temp_c": final_temp,
                    "elapsed_s": time.monotonic() - started,
                    "timed_out": True,
                    "hold_s": hold_s,
                    "stability_c": stability_c,
                    "window_min_c": min(window_temps) if window_temps else None,
                    "window_max_c": max(window_temps) if window_temps else None,
                    "window_sample_count": len(window_samples),
                }

            if action == "cool":
                stop_warm_processes()
                window_samples = []
                fan.write_pwm(cool_pwm)
            elif action == "warm":
                window_samples = []
                fan.write_pwm(warm_pwm)
                if not warm_processes or all(process.poll() is not None for process in warm_processes):
                    from random_stress_test import StressPhase

                    warm_processes = start_load(StressPhase(index=0, duration_s=20, cpu_workers=1))
            else:
                stop_warm_processes()
                window_samples = []
                fan.write_pwm(start_pwm)
            time.sleep(2.0)
    finally:
        stop_warm_processes()


def run_mode(
    args: argparse.Namespace,
    run_dir: Path,
    fan: SysfsFan,
    plan_mode: str,
    schedule: list[Any],
) -> dict[str, Any]:
    csv_path = run_dir / f"{plan_mode}.csv"
    controller = None if plan_mode == "official-step" else build_controller(args, plan_mode)
    load_meter = CpuLoadMeter()
    prediction_observer = PredictionObserver()
    prev_temp_c: float | None = None
    prev_pwm: int | None = None
    prev_load: float | None = None
    rows: list[dict[str, Any]] = []
    load_results: list[dict[str, Any]] = []
    process_started = time.process_time()
    started_mono = time.monotonic()

    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "timestamp",
            "elapsed_s",
            "plan_mode",
            "phase_index",
            "phase_workers",
            "temp_c",
            "load",
            "current_pwm",
            "pwm",
            "rpm",
            "freq_mhz",
            "pred_error_c",
            "bias_ewma_c",
            "rmse_ewma_c",
            "prediction_margin_c",
            "predicted_max_temp_c",
            "terminal_temp_c",
            "violation_steps",
            "violation_area_c_steps",
            "cost",
            "reason",
            "planned_pwms",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()

        for phase in schedule:
            print(
                f"[{plan_mode}] phase={phase.index} workers={phase.cpu_workers} duration={phase.duration_s}s",
                flush=True,
            )
            phase_deadline = time.monotonic() + phase.duration_s
            processes = start_load(phase)
            try:
                while True:
                    now = time.monotonic()
                    temp_c = fan.read_temp_c()
                    current_pwm = fan.read_pwm()
                    rpm = fan.read_rpm()
                    load = load_meter.read()
                    stats = prediction_observer.observe(temp_c)
                    if plan_mode == "official-step":
                        pwm = official_step_pwm(temp_c, current_pwm, pwm_scale=args.official_pwm_scale)
                        predicted_max_temp_c = temp_c
                        terminal_temp_c = temp_c
                        violation_steps = 1 if temp_c > args.zone_high else 0
                        violation_area = max(0.0, temp_c - args.zone_high)
                        cost = 0.0
                        reason = "official_step"
                        planned_pwms = [pwm]
                    else:
                        assert controller is not None
                        decision = controller.decide(
                            temp_c=temp_c,
                            load=load,
                            current_pwm=current_pwm,
                            prediction_margin_c=stats.prediction_margin_c,
                            prev_temp_c=prev_temp_c,
                            prev_pwm=prev_pwm,
                            prev_load=prev_load,
                        )
                        pwm = decision.pwm
                        predicted_max_temp_c = decision.predicted_max_temp_c
                        terminal_temp_c = decision.terminal_temp_c
                        violation_steps = decision.violation_steps
                        violation_area = decision.violation_area_c_steps
                        cost = decision.cost
                        reason = decision.reason
                        planned_pwms = decision.planned_pwms
                        prediction_observer.record_prediction(
                            controller.model.predict_with_state(
                                temp_c=temp_c,
                                pwm=pwm,
                                load=load,
                                prev_temp_c=temp_c if prev_temp_c is None else prev_temp_c,
                                prev_pwm=current_pwm if prev_pwm is None else prev_pwm,
                                prev_load=load if prev_load is None else prev_load,
                            )
                        )
                    fan.write_pwm(pwm)
                    row = {
                        "timestamp": f"{time.time():.3f}",
                        "elapsed_s": now - started_mono,
                        "plan_mode": plan_mode,
                        "phase_index": phase.index,
                        "phase_workers": phase.cpu_workers,
                        "temp_c": temp_c,
                        "load": load,
                        "current_pwm": current_pwm,
                        "pwm": pwm,
                        "rpm": rpm,
                        "freq_mhz": fan.read_freq_mhz(),
                        "pred_error_c": "" if stats.pred_error_c is None else stats.pred_error_c,
                        "bias_ewma_c": stats.bias_ewma_c,
                        "rmse_ewma_c": stats.rmse_ewma_c,
                        "prediction_margin_c": stats.prediction_margin_c,
                        "predicted_max_temp_c": predicted_max_temp_c,
                        "terminal_temp_c": terminal_temp_c,
                        "violation_steps": violation_steps,
                        "violation_area_c_steps": violation_area,
                        "cost": cost,
                        "reason": reason,
                        "planned_pwms": json.dumps(planned_pwms),
                    }
                    rows.append(row)
                    writer.writerow(row)
                    handle.flush()
                    if temp_c >= args.abort_temp:
                        fan.write_pwm(255)
                        raise RuntimeError(f"{plan_mode} reached abort temp {temp_c:.2f} C")
                    prev_temp_c = temp_c
                    prev_pwm = pwm
                    prev_load = load
                    if now >= phase_deadline:
                        break
                    time.sleep(min(args.interval, max(0.0, phase_deadline - now)))
            finally:
                load_results.extend(finish_load(processes))

    summary = summarize_samples(rows, args.zone_high)
    summary.update(
        {
            "plan_mode": plan_mode,
            "csv_path": str(csv_path),
            "process_cpu_seconds": time.process_time() - process_started,
            "load_results_tail": load_results[-10:],
        }
    )
    return {"samples": rows, "summary": summary}


def write_svg(path: Path, constant: list[dict[str, Any]], segmented: list[dict[str, Any]]) -> None:
    series = [
        ("temp_c", "Temperature C", "#c2410c"),
        ("pwm", "PWM", "#2563eb"),
        ("rpm", "RPM", "#047857"),
    ]
    width = 960
    chart_h = 180
    left = 72
    right = 24
    top = 32
    gap = 42
    height = top + len(series) * chart_h + (len(series) - 1) * gap + 36
    all_samples = constant + segmented
    max_elapsed = max((float(sample["elapsed_s"]) for sample in all_samples), default=1.0)

    def points(samples: list[dict[str, Any]], key: str, lo: float, hi: float, y0: float) -> str:
        span = max(hi - lo, 1.0)
        out = []
        for sample in samples:
            x = left + (float(sample["elapsed_s"]) / max_elapsed) * (width - left - right)
            y = y0 + chart_h - ((float(sample[key]) - lo) / span) * chart_h
            out.append(f"{x:.1f},{y:.1f}")
        return " ".join(out)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#fafafa"/>',
        '<text x="24" y="22" font-family="sans-serif" font-size="16" font-weight="700">Zone MPC AB Test Curves</text>',
        '<text x="760" y="22" font-family="sans-serif" font-size="12" fill="#555">constant black, segmented color</text>',
    ]
    for index, (key, label, color) in enumerate(series):
        y0 = top + index * (chart_h + gap)
        values = [float(sample[key]) for sample in all_samples]
        lo = math.floor(min(values) - 1) if values else 0.0
        hi = math.ceil(max(values) + 1) if values else 1.0
        if key == "pwm":
            lo, hi = 0.0, 255.0
        parts.extend(
            [
                f'<line x1="{left}" y1="{y0 + chart_h}" x2="{width - right}" y2="{y0 + chart_h}" stroke="#ddd"/>',
                f'<line x1="{left}" y1="{y0}" x2="{left}" y2="{y0 + chart_h}" stroke="#ddd"/>',
                f'<text x="18" y="{y0 + 16}" font-family="sans-serif" font-size="13" font-weight="700">{label}</text>',
                f'<text x="24" y="{y0 + chart_h}" font-family="sans-serif" font-size="11" fill="#777">{lo:g}</text>',
                f'<text x="24" y="{y0 + 10}" font-family="sans-serif" font-size="11" fill="#777">{hi:g}</text>',
                f'<polyline fill="none" stroke="#111827" stroke-width="2" points="{points(constant, key, lo, hi, y0)}"/>',
                f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{points(segmented, key, lo, hi, y0)}"/>',
            ]
        )
    parts.append("</svg>\n")
    path.write_text("\n".join(parts), encoding="utf-8")


def render_report(result: dict[str, Any]) -> str:
    left_mode, right_mode = result["modes"]
    constant = result[left_mode]["summary"]
    segmented = result[right_mode]["summary"]
    fairness = result["fairness"]
    schedule = result["schedule"]

    def fmt(value: Any, digits: int = 2) -> str:
        if value is None:
            return "n/a"
        if isinstance(value, float):
            return f"{value:.{digits}f}"
        return str(value)

    def row(label: str, key: str, digits: int = 2) -> str:
        return f"| {label} | {fmt(constant.get(key), digits)} | {fmt(segmented.get(key), digits)} |\n"

    schedule_text = ", ".join(f"{item['cpu_workers']}c/{item['duration_s']}s" for item in schedule)
    conclusion = "inconclusive"
    if segmented["terminal_mean_c"] < constant["terminal_mean_c"] and segmented["over_zone_seconds"] <= constant[
        "over_zone_seconds"
    ]:
        conclusion = "segmented showed stronger thermal-margin planning"
    elif segmented["over_zone_seconds"] > constant["over_zone_seconds"]:
        conclusion = "constant held the zone better in this run"

    return (
        "# Constant vs Segmented Zone MPC AB Test\n\n"
        f"Run directory: `{result['run_dir']}`\n\n"
        f"- Seed: `{result['seed']}`\n"
        f"- Duration per arm: `{result['duration_s']}s`\n"
        f"- Zone: `{result['zone_low_c']}-{result['zone_high_c']} C`\n"
        f"- Requested start target: `{result['start_target_c']} ± {result['start_tolerance_c']} C`\n"
        f"- Segmented start target: `{fmt(result.get('segmented_start_target_c'))} C`\n"
        f"- {left_mode} precondition: `{fmt(result[left_mode]['precondition']['temp_c'])} C`, "
        f"{right_mode} precondition: `{fmt(result[right_mode]['precondition']['temp_c'])} C`\n"
        f"- Start sample delta: `{fmt(fairness['start_temp_delta_abs_c'])} C`\n"
        f"- Mean load delta: `{fmt(fairness['load_mean_delta_abs'], 3)}`; "
        f"phase-load RMSE: `{fmt(fairness['phase_load_rmse'], 3)}` over "
        f"`{fairness['common_phase_count']}` common phases\n"
        f"- Schedule: `{schedule_text}`\n"
        f"- Conclusion: `{conclusion}`\n\n"
        "![AB curves](ab_curves.svg)\n\n"
        "## Metrics\n\n"
        f"| Metric | {left_mode} | {right_mode} |\n"
        "|---|---:|---:|\n"
        + row("Samples", "sample_count", 0)
        + row("Temperature start C", "temp_start_c")
        + row("Temperature peak C", "temp_peak_c")
        + row("Temperature mean C", "temp_mean_c")
        + row("Average measured load", "load_mean", 3)
        + row("Peak measured load", "load_peak", 3)
        + row("Seconds above zone_high", "over_zone_seconds")
        + row("Percent above zone_high", "over_zone_percent")
        + row("Predicted max peak C", "predicted_max_peak_c")
        + row("Predicted max mean C", "predicted_max_mean_c")
        + row("Terminal peak C", "terminal_peak_c")
        + row("Terminal mean C", "terminal_mean_c")
        + row("Average PWM", "pwm_mean")
        + row("Peak PWM", "pwm_peak")
        + row("PWM change count", "pwm_change_count", 0)
        + row("PWM reversal count", "pwm_reversal_count", 0)
        + row("Average RPM", "rpm_mean")
        + row("Peak RPM", "rpm_peak")
        + row("Controller CPU seconds", "process_cpu_seconds", 4)
        + "\n"
        "CSV files contain per-sample `predicted_max_temp_c`, `terminal_temp_c`, `planned_pwms`, PWM and RPM.\n"
    )


def main() -> int:
    args = parse_args()
    ensure_root_for_writes()
    run_name = f"ab-mpc-{time.strftime('%Y%m%d-%H%M%S')}"
    run_dir = Path(args.output_dir) / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    run_dir.chmod(0o755)

    schedule = build_random_schedule(args.duration, args.min_phase, args.max_phase, args.max_workers, args.seed)
    modes = parse_modes(args.modes)
    fan = SysfsFan.discover()
    fan.set_manual()
    service_was_active = service_is_active(args.service)
    result: dict[str, Any] = {
        "run_dir": str(run_dir),
        "seed": args.seed,
        "duration_s": args.duration,
        "zone_low_c": args.zone_low,
        "zone_high_c": args.zone_high,
        "start_target_c": args.start_temp,
        "start_tolerance_c": args.start_temp_tolerance,
        "service": args.service,
        "service_was_active": service_was_active,
        "schedule": [asdict(phase) for phase in schedule],
        "modes": modes,
    }

    try:
        if service_was_active:
            print(f"Stopping {args.service} for exclusive PWM control", flush=True)
            systemctl("stop", args.service)

        arm_start_target = args.start_temp
        arm_samples: dict[str, list[dict[str, Any]]] = {}
        for index, plan_mode in enumerate(modes):
            print(f"Preconditioning {plan_mode} near {arm_start_target:.2f} C", flush=True)
            precondition = precondition_start_temp(
                fan=fan,
                target_c=arm_start_target,
                tolerance_c=args.start_temp_tolerance,
                timeout_s=args.precondition_timeout,
                start_pwm=args.start_pwm,
                hold_s=args.start_hold_s,
                stability_c=args.start_stability_c,
                settle_s=args.start_settle_s,
                cool_pwm=args.cool_pwm,
                warm_pwm=args.warm_pwm,
                mode=args.precondition_mode,
            )
            if precondition["timed_out"] and not args.allow_precondition_timeout:
                raise RuntimeError(
                    f"{plan_mode} preconditioning timed out at {precondition['temp_c']:.2f} C; "
                    "increase timeout/tolerance or rerun when ambient conditions are steadier"
                )
            print(f"Starting {plan_mode} at {precondition['temp_c']:.2f} C", flush=True)
            arm = run_mode(args, run_dir, fan, plan_mode, schedule)
            result[plan_mode] = {"precondition": precondition, "summary": arm["summary"]}
            arm_samples[plan_mode] = arm["samples"]
            if index == 0:
                arm_start_target = float(arm["summary"]["temp_start_c"])
                result["segmented_start_target_c"] = arm_start_target

        result["fairness"] = compare_arm_fairness(result[modes[0]]["summary"], result[modes[1]]["summary"])
        write_svg(run_dir / "ab_curves.svg", arm_samples[modes[0]], arm_samples[modes[1]])
        (run_dir / "ab_summary.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        (run_dir / "report.md").write_text(render_report(result), encoding="utf-8")
        restore_sudo_owner(run_dir)
        print(json.dumps({"run_dir": str(run_dir), modes[0]: result[modes[0]], modes[1]: result[modes[1]]}, indent=2))
        print(f"Wrote report to {run_dir / 'report.md'}", flush=True)
        return 0
    finally:
        fan.write_pwm(255)
        if service_was_active:
            print(f"Restoring {args.service}", flush=True)
            systemctl("start", args.service)


if __name__ == "__main__":
    raise SystemExit(main())
