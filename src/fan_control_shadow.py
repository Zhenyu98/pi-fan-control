from __future__ import annotations

import csv
import os
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from fan_control_core import (
    Arx2ThermalModel,
    Sample,
    ThermalModel,
    ThermalPredictor,
    arx2_training_rows,
    fit_arx2_thermal_model,
    fit_thermal_model,
    paired_rows,
)


@dataclass(frozen=True)
class ShadowConfig:
    min_samples: int = 240
    window_samples: int = 900
    check_interval_s: float = 1800.0
    min_improvement: float = 0.08
    min_pwm_span: int = 40
    min_load_span: float = 0.20
    min_temp_span_c: float = 2.0
    max_param_change: float = 0.75
    log_path: Path = Path("/home/pi/fan-control/data/shadow_samples.csv")
    max_log_bytes: int = 5_000_000


@dataclass(frozen=True)
class ShadowDecision:
    accepted: bool
    reason: str
    current_mae: float
    candidate_mae: float
    candidate: ThermalPredictor


def prediction_mae(model: ThermalPredictor, rows: Iterable[dict[str, float]]) -> float:
    errors = []
    for row in rows:
        predicted = model.predict_with_state(
            temp_c=row["temp_c"],
            pwm=row["pwm"],
            load=row["load"],
            prev_temp_c=row.get("temp_prev_c", row["temp_c"]),
            prev_pwm=row.get("pwm_prev", row["pwm"]),
            prev_load=row.get("load_prev", row["load"]),
        )
        errors.append(abs(predicted - row["next_temp_c"]))
    if not errors:
        return float("inf")
    return sum(errors) / len(errors)


def prediction_bias(model: ThermalPredictor, rows: Iterable[dict[str, float]]) -> float:
    errors = []
    for row in rows:
        predicted = model.predict_with_state(
            temp_c=row["temp_c"],
            pwm=row["pwm"],
            load=row["load"],
            prev_temp_c=row.get("temp_prev_c", row["temp_c"]),
            prev_pwm=row.get("pwm_prev", row["pwm"]),
            prev_load=row.get("load_prev", row["load"]),
        )
        errors.append(predicted - row["next_temp_c"])
    if not errors:
        return 0.0
    return sum(errors) / len(errors)


def _span(values: Iterable[float]) -> float:
    collected = list(values)
    if not collected:
        return 0.0
    return max(collected) - min(collected)


def _predictor_parameters(model: ThermalPredictor) -> list[float]:
    if isinstance(model, ThermalModel):
        return [model.a, model.b, model.c, model.d]
    if isinstance(model, Arx2ThermalModel):
        return [
            model.temp_c,
            model.temp_prev_c,
            model.pwm,
            model.pwm_prev,
            model.load,
            model.load_prev,
            model.bias,
        ]
    raise TypeError(f"unsupported shadow model type: {type(model).__name__}")


def _max_relative_change(current: ThermalPredictor, candidate: ThermalPredictor) -> float:
    current_values = _predictor_parameters(current)
    candidate_values = _predictor_parameters(candidate)
    if len(current_values) != len(candidate_values):
        return float("inf")
    changes = []
    for old_value, new_value in zip(current_values, candidate_values):
        old = abs(old_value)
        new = abs(new_value)
        denominator = max(old, 0.05)
        changes.append(abs(new - old) / denominator)
    return max(changes)


def _validate_first_order_candidate(candidate: ThermalModel) -> str | None:
    if not (0.60 <= candidate.a <= 1.10):
        return f"unsafe a coefficient: {candidate.a:.6f}"
    if not (-0.20 <= candidate.b < -0.0005):
        return f"unsafe b coefficient: {candidate.b:.6f}"
    if not (-5.0 <= candidate.c <= 8.0):
        return f"unsafe c coefficient: {candidate.c:.6f}"
    return None


def _validate_arx2_candidate(candidate: Arx2ThermalModel) -> str | None:
    temp_memory = candidate.temp_c + candidate.temp_prev_c
    if not (0.75 <= temp_memory <= 1.08):
        return f"unsafe ARX temperature memory: {temp_memory:.6f}"
    if not (-0.05 <= candidate.pwm_coefficient < -0.0001):
        return f"unsafe ARX PWM coefficient: {candidate.pwm_coefficient:.6f}"
    if not (0.02 <= candidate.load_coefficient <= 3.0):
        return f"unsafe ARX load coefficient: {candidate.load_coefficient:.6f}"
    if not (-20.0 <= candidate.bias <= 20.0):
        return f"unsafe ARX bias: {candidate.bias:.6f}"
    for name in ("temp_c", "temp_prev_c", "load", "load_prev"):
        value = getattr(candidate, name)
        if not (-3.0 <= value <= 3.0):
            return f"unsafe ARX {name} coefficient: {value:.6f}"
    return None


def evaluate_candidate_model(
    current_model: ThermalPredictor,
    samples: Iterable[Sample],
    config: ShadowConfig,
) -> ShadowDecision:
    sample_list = list(samples)
    fallback = current_model
    if len(sample_list) < config.min_samples:
        return ShadowDecision(False, "not enough samples", float("inf"), float("inf"), fallback)

    pwm_span = _span(sample.pwm for sample in sample_list)
    if pwm_span < config.min_pwm_span:
        return ShadowDecision(False, f"pwm span too small: {pwm_span:.1f}", float("inf"), float("inf"), fallback)

    load_span = _span(sample.load for sample in sample_list)
    if load_span < config.min_load_span:
        return ShadowDecision(False, f"load span too small: {load_span:.3f}", float("inf"), float("inf"), fallback)

    temp_span = _span(sample.temp_c for sample in sample_list)
    if temp_span < config.min_temp_span_c:
        return ShadowDecision(False, f"temperature span too small: {temp_span:.2f}", float("inf"), float("inf"), fallback)

    is_arx2 = isinstance(current_model, Arx2ThermalModel)
    rows = arx2_training_rows(sample_list) if is_arx2 else paired_rows(sample_list)
    try:
        candidate: ThermalPredictor
        if is_arx2:
            candidate = fit_arx2_thermal_model(sample_list)
        else:
            candidate = fit_thermal_model(rows)
    except ValueError as exc:
        return ShadowDecision(False, str(exc), float("inf"), float("inf"), fallback)

    safety_error = (
        _validate_arx2_candidate(candidate)
        if isinstance(candidate, Arx2ThermalModel)
        else _validate_first_order_candidate(candidate)
    )
    if safety_error is not None:
        return ShadowDecision(False, safety_error, float("inf"), float("inf"), candidate)

    high_temp_rows = [row for row in rows if row["temp_c"] > 60.0]
    if len(high_temp_rows) >= max(12, config.min_samples // 5):
        high_temp_bias = prediction_bias(candidate, high_temp_rows)
        if high_temp_bias < -0.5:
            return ShadowDecision(
                False,
                f"high-temperature bias too negative: {high_temp_bias:.3f}",
                float("inf"),
                float("inf"),
                candidate,
            )

    current_mae = prediction_mae(current_model, rows)
    candidate_mae = prediction_mae(candidate, rows)
    if candidate_mae >= current_mae * (1.0 - config.min_improvement):
        return ShadowDecision(
            False,
            f"candidate MAE {candidate_mae:.4f} did not improve enough over {current_mae:.4f}",
            current_mae,
            candidate_mae,
            candidate,
        )

    param_change = _max_relative_change(current_model, candidate)
    if param_change > config.max_param_change:
        return ShadowDecision(
            False,
            f"parameter change too large: {param_change:.3f}",
            current_mae,
            candidate_mae,
            candidate,
        )

    return ShadowDecision(True, "accepted", current_mae, candidate_mae, candidate)


def promote_model_atomically(model: ThermalModel | Arx2ThermalModel, model_path: str | Path) -> None:
    target = Path(model_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = target.with_suffix(".previous.json")
    temporary = target.with_suffix(".tmp")
    model.save(temporary)
    temporary.chmod(0o644)
    if target.exists():
        os.replace(target, backup)
        backup.chmod(0o644)
    os.replace(temporary, target)
    target.chmod(0o644)


class ShadowLearner:
    def __init__(self, config: ShadowConfig) -> None:
        self.config = config
        self.samples: deque[Sample] = deque(maxlen=config.window_samples)
        self.last_check_monotonic = time.monotonic()
        self._log_header_written = config.log_path.exists()

    def observe(self, sample: Sample) -> None:
        self.samples.append(sample)
        self._append_log(sample)

    def maybe_promote(
        self,
        current_model: ThermalPredictor,
        model_path: str | Path,
        force: bool = False,
    ) -> ShadowDecision | None:
        now = time.monotonic()
        if not force and now - self.last_check_monotonic < self.config.check_interval_s:
            return None
        self.last_check_monotonic = now

        decision = evaluate_candidate_model(current_model, list(self.samples), self.config)
        if decision.accepted:
            if not isinstance(decision.candidate, (ThermalModel, Arx2ThermalModel)):
                raise TypeError("accepted shadow candidate must be a savable thermal model")
            promote_model_atomically(decision.candidate, model_path)
        return decision

    def _append_log(self, sample: Sample) -> None:
        path = self.config.log_path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._rotate_log_if_needed()
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["timestamp", "temp_c", "pwm", "rpm", "load", "freq_mhz"],
            )
            if not self._log_header_written:
                writer.writeheader()
                self._log_header_written = True
            writer.writerow(
                {
                    "timestamp": f"{sample.timestamp:.3f}",
                    "temp_c": f"{sample.temp_c:.3f}",
                    "pwm": sample.pwm,
                    "rpm": sample.rpm,
                    "load": f"{sample.load:.4f}",
                    "freq_mhz": f"{sample.freq_mhz:.1f}",
                }
            )

    def _rotate_log_if_needed(self) -> None:
        path = self.config.log_path
        if self.config.max_log_bytes <= 0 or not path.exists():
            return
        if path.stat().st_size < self.config.max_log_bytes:
            return
        previous = path.with_suffix(path.suffix + ".1")
        if previous.exists():
            previous.unlink()
        os.replace(path, previous)
        previous.chmod(0o644)
        self._log_header_written = False
