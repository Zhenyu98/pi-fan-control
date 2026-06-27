#!/usr/bin/env python3
from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

from fan_control_core import (
    Arx2ThermalModel,
    PredictionObserver,
    Sample,
    ThermalPredictor,
    ZoneMpcController,
    load_predictor,
)
from fan_control_io import CpuLoadMeter, SysfsFan, ensure_root_for_writes
from fan_control_shadow import ShadowConfig, ShadowLearner


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Raspberry Pi fan ARX2 Zone MPC controller.")
    parser.add_argument("--model", default="/home/pi/fan-control/data/model_arx2_m2.json")
    parser.add_argument("--control-mode", choices=("zone-mpc",), default="zone-mpc")
    parser.add_argument("--zone-low", type=float, default=53.0, help="Zone MPC lower comfort bound in Celsius")
    parser.add_argument("--zone-high", type=float, default=58.0, help="Zone MPC upper comfort bound in Celsius")
    parser.add_argument("--full-temp", type=float, default=69.0, help="Zone MPC full-speed threshold in Celsius")
    parser.add_argument("--mpc-horizon", type=int, default=12, help="Zone MPC prediction horizon in control steps")
    parser.add_argument("--mpc-candidate-step", type=int, default=5, help="Zone MPC PWM candidate spacing")
    parser.add_argument("--mpc-plan-mode", choices=("constant", "segmented"), default="constant")
    parser.add_argument("--mpc-segments", type=int, default=3)
    parser.add_argument("--mpc-segment-candidate-step", type=int, default=20)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--duration", type=float, default=0.0, help="0 means run until stopped")
    parser.add_argument("--max-step", type=int, default=20)
    parser.add_argument("--min-active-pwm", type=int, default=75)
    parser.add_argument("--idle-stop", type=float, default=50.0)
    parser.add_argument("--safety-temp", type=float, default=70.0)
    parser.add_argument("--safety-pwm", type=int, default=255)
    parser.add_argument("--shadow-learn", action="store_true", help="Record samples and safely promote better fitted models")
    parser.add_argument("--shadow-window-samples", type=int, default=900)
    parser.add_argument("--shadow-min-samples", type=int, default=240)
    parser.add_argument("--shadow-check-interval", type=float, default=1800.0)
    parser.add_argument("--shadow-min-improvement", type=float, default=0.08)
    parser.add_argument("--shadow-log", default="/home/pi/fan-control/data/shadow_samples.csv")
    parser.add_argument("--log-interval", type=float, default=30.0, help="Minimum seconds between routine status logs")
    parser.add_argument("--dry-run", action="store_true", help="Print decisions without writing fan PWM")
    parser.add_argument("--no-restore-auto", action="store_true", help="Leave manual mode on exit")
    return parser.parse_args()


def load_model(path: str) -> ThermalPredictor:
    model_path = Path(path)
    try:
        if model_path.exists():
            return load_predictor(model_path)
    except PermissionError:
        print(f"warning: cannot read {model_path}; using identified M2 ARX2 model", file=sys.stderr)
    return Arx2ThermalModel.m2_identified()


def build_controller(args: argparse.Namespace, model: ThermalPredictor) -> ZoneMpcController:
    return ZoneMpcController(
        model=model,
        zone_low_temp_c=args.zone_low,
        zone_high_temp_c=args.zone_high,
        full_temp_c=args.full_temp,
        max_pwm_step=args.max_step,
        min_active_pwm=args.min_active_pwm,
        idle_stop_temp_c=args.idle_stop,
        safety_temp_c=args.safety_temp,
        safety_pwm=args.safety_pwm,
        horizon_steps=args.mpc_horizon,
        candidate_pwm_step=args.mpc_candidate_step,
        plan_mode=args.mpc_plan_mode,
        segments=args.mpc_segments,
        segment_candidate_step=args.mpc_segment_candidate_step,
    )


def control_label(args: argparse.Namespace) -> str:
    return f"zone={args.zone_low:.1f}-{args.zone_high:.1f}C full={args.full_temp:.1f}C"


def should_log_decision(
    now_monotonic: float,
    last_log_monotonic: float | None,
    log_interval_s: float,
    current_pwm: int,
    next_pwm: int,
    reason: str,
) -> bool:
    if log_interval_s <= 0:
        return True
    if last_log_monotonic is None:
        return True
    if next_pwm != current_pwm:
        return True
    if reason not in {"inside_zone", "idle_stop", "brief_violation"}:
        return True
    return now_monotonic - last_log_monotonic >= log_interval_s


def main() -> int:
    args = parse_args()
    if not args.dry_run:
        ensure_root_for_writes()

    fan = SysfsFan.discover()
    load_meter = CpuLoadMeter()
    model = load_model(args.model)
    controller = build_controller(args, model)
    prediction_observer = PredictionObserver()
    prev_temp_c: float | None = None
    prev_pwm: int | None = None
    prev_load: float | None = None
    last_log_monotonic: float | None = None
    learner = None
    if args.shadow_learn and not args.dry_run:
        learner = ShadowLearner(
            ShadowConfig(
                min_samples=args.shadow_min_samples,
                window_samples=args.shadow_window_samples,
                check_interval_s=args.shadow_check_interval,
                min_improvement=args.shadow_min_improvement,
                log_path=Path(args.shadow_log),
            )
        )
    stop = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    if not args.dry_run:
        fan.set_manual()

    deadline = None if args.duration <= 0 else time.monotonic() + args.duration
    try:
        while not stop:
            now_monotonic = time.monotonic()
            temp_c = fan.read_temp_c()
            current_pwm = fan.read_pwm()
            rpm = fan.read_rpm()
            load = load_meter.read()
            prediction_stats = prediction_observer.observe(temp_c)
            mpc_decision = controller.decide(
                temp_c=temp_c,
                load=load,
                current_pwm=current_pwm,
                prediction_margin_c=prediction_stats.prediction_margin_c,
                prev_temp_c=prev_temp_c,
                prev_pwm=prev_pwm,
                prev_load=prev_load,
            )
            next_pwm = mpc_decision.pwm
            prediction_observer.record_prediction(
                model.predict_with_state(
                    temp_c=temp_c,
                    pwm=next_pwm,
                    load=load,
                    prev_temp_c=temp_c if prev_temp_c is None else prev_temp_c,
                    prev_pwm=current_pwm if prev_pwm is None else prev_pwm,
                    prev_load=load if prev_load is None else prev_load,
                )
            )

            if not args.dry_run:
                fan.write_pwm(next_pwm)
                if learner is not None:
                    sample = Sample(
                        timestamp=time.time(),
                        temp_c=temp_c,
                        pwm=next_pwm,
                        rpm=rpm,
                        load=load,
                        freq_mhz=fan.read_freq_mhz(),
                    )
                    learner.observe(sample)
                    learner_decision = learner.maybe_promote(model, args.model)
                    if learner_decision is not None:
                        print(
                            f"shadow-learning: {learner_decision.reason}; "
                            f"current_mae={learner_decision.current_mae:.4f} "
                            f"candidate_mae={learner_decision.candidate_mae:.4f}",
                            flush=True,
                        )
                        if learner_decision.accepted:
                            model = learner_decision.candidate
                            controller = build_controller(args, model)
                            prediction_observer = PredictionObserver()
                            prev_temp_c = None
                            prev_pwm = None
                            prev_load = None
                            print("shadow-learning: promoted model and hot-loaded controller", flush=True)

            if should_log_decision(
                now_monotonic=now_monotonic,
                last_log_monotonic=last_log_monotonic,
                log_interval_s=args.log_interval,
                current_pwm=current_pwm,
                next_pwm=next_pwm,
                reason=mpc_decision.reason,
            ):
                log_line = (
                    f"temp={temp_c:.2f}C load={load:.2f} pwm={current_pwm}->{next_pwm} "
                    f"rpm={rpm} mode={args.control_mode} plan_mode={mpc_decision.plan_mode} "
                    f"{control_label(args)} "
                    f"pred_error={_format_optional(prediction_stats.pred_error_c)}C "
                    f"bias_ewma={prediction_stats.bias_ewma_c:.3f}C "
                    f"rmse_ewma={prediction_stats.rmse_ewma_c:.3f}C "
                    f"prediction_margin={prediction_stats.prediction_margin_c:.3f}C"
                )
                log_line += (
                    f" predicted_max={mpc_decision.predicted_max_temp_c:.2f}C "
                    f"terminal={mpc_decision.terminal_temp_c:.2f}C "
                    f"violation_steps={mpc_decision.violation_steps} "
                    f"violation_area={mpc_decision.violation_area_c_steps:.2f} "
                    f"reason={mpc_decision.reason}"
                )
                if mpc_decision.plan_mode == "segmented":
                    log_line += f" plan={mpc_decision.planned_pwms}"
                print(log_line, flush=True)
                last_log_monotonic = now_monotonic
            prev_temp_c = temp_c
            prev_pwm = next_pwm
            prev_load = load

            if deadline is not None and time.monotonic() >= deadline:
                break
            time.sleep(args.interval)
    finally:
        if not args.dry_run and not args.no_restore_auto:
            try:
                fan.restore_auto()
            except Exception as exc:  # noqa: BLE001
                print(f"warning: failed to restore automatic fan mode: {exc}", file=sys.stderr)

    return 0


def _format_optional(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.3f}"


if __name__ == "__main__":
    raise SystemExit(main())
