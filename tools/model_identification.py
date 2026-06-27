from __future__ import annotations

import _pathfix  # noqa: F401  # adds ../src to sys.path; must precede src imports

import math
import statistics
from dataclasses import dataclass
from typing import Any, Iterable, Protocol

from fan_control_core import ThermalModel


PWM_LEVELS = [0, 75, 100, 130, 160, 200, 255]
LOAD_LEVELS = [("idle", 0), ("cpu2", 2), ("cpu4", 4)]


@dataclass(frozen=True)
class ExperimentPhase:
    index: int
    load_name: str
    cpu_workers: int
    scheduled_pwm: int
    duration_s: int

    @staticmethod
    def sample_dict(temp_c: float, pwm: float, load: float, rpm: float, freq_mhz: float) -> dict[str, float]:
        return {
            "temp_c": temp_c,
            "pwm": pwm,
            "load": load,
            "rpm": rpm,
            "freq_mhz": freq_mhz,
        }


class PredictiveModel(Protocol):
    name: str
    pwm_coefficient: float | None
    load_coefficient: float | None

    def predict_next(self, samples: list[dict[str, float]], index: int, temp_c: float) -> float:
        ...

    def parameters(self) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class LinearThermalModel:
    name: str
    coefficients: list[float]
    feature_names: list[str]

    @property
    def pwm_coefficient(self) -> float | None:
        total = 0.0
        found = False
        for name, coefficient in zip(self.feature_names, self.coefficients):
            if "pwm" in name:
                total += coefficient
                found = True
        return total if found else None

    @property
    def load_coefficient(self) -> float | None:
        total = 0.0
        found = False
        for name, coefficient in zip(self.feature_names, self.coefficients):
            if "load" in name:
                total += coefficient
                found = True
        return total if found else None

    def predict_next(self, samples: list[dict[str, float]], index: int, temp_c: float) -> float:
        sample = samples[index]
        previous = samples[index - 1] if index > 0 else sample
        values = {
            "temp_c": temp_c,
            "temp_prev_c": float(previous["temp_c"]),
            "pwm": float(sample["pwm"]),
            "pwm_prev": float(previous["pwm"]),
            "load": float(sample["load"]),
            "load_prev": float(previous["load"]),
            "rpm": float(sample.get("rpm", 0.0)),
            "freq_mhz": float(sample.get("freq_mhz", 0.0)),
            "bias": 1.0,
        }
        return sum(coefficient * values[name] for name, coefficient in zip(self.feature_names, self.coefficients))

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "linear",
            "feature_names": self.feature_names,
            "coefficients": self.coefficients,
            "pwm_coefficient": self.pwm_coefficient,
            "load_coefficient": self.load_coefficient,
        }


@dataclass(frozen=True)
class DefaultModelAdapter:
    model: ThermalModel
    name: str = "M0 default"

    @property
    def pwm_coefficient(self) -> float:
        return self.model.b

    @property
    def load_coefficient(self) -> float:
        return self.model.c

    def predict_next(self, samples: list[dict[str, float]], index: int, temp_c: float) -> float:
        sample = samples[index]
        return self.model.predict(temp_c, float(sample["pwm"]), float(sample["load"]))

    def parameters(self) -> dict[str, Any]:
        return {"type": "default", "a": self.model.a, "b": self.model.b, "c": self.model.c, "d": self.model.d}


@dataclass(frozen=True)
class PiecewiseLinearModel:
    name: str
    split_temp_c: float
    low_model: LinearThermalModel
    high_model: LinearThermalModel

    @property
    def pwm_coefficient(self) -> float:
        return max(self.low_model.pwm_coefficient or 0.0, self.high_model.pwm_coefficient or 0.0)

    @property
    def load_coefficient(self) -> float:
        return min(self.low_model.load_coefficient or 0.0, self.high_model.load_coefficient or 0.0)

    def predict_next(self, samples: list[dict[str, float]], index: int, temp_c: float) -> float:
        model = self.high_model if temp_c >= self.split_temp_c else self.low_model
        return model.predict_next(samples, index, temp_c)

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "piecewise_linear",
            "split_temp_c": self.split_temp_c,
            "low": self.low_model.parameters(),
            "high": self.high_model.parameters(),
            "pwm_coefficient": self.pwm_coefficient,
            "load_coefficient": self.load_coefficient,
        }


def build_identification_schedule(phase_duration_s: int = 180) -> list[ExperimentPhase]:
    phases: list[ExperimentPhase] = []
    index = 1
    for load_name, workers in LOAD_LEVELS:
        pwm_sequence = PWM_LEVELS if load_name != "cpu4" else list(reversed(PWM_LEVELS))
        for pwm in pwm_sequence:
            phases.append(
                ExperimentPhase(
                    index=index,
                    load_name=load_name,
                    cpu_workers=workers,
                    scheduled_pwm=pwm,
                    duration_s=phase_duration_s,
                )
            )
            index += 1
    return phases


def paired_samples(samples: list[dict[str, float]]) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    for index, (current, following) in enumerate(zip(samples, samples[1:])):
        row = dict(current)
        row["next_temp_c"] = float(following["temp_c"])
        row["index"] = float(index)
        rows.append(row)
    return rows


def fit_constrained_linear(rows: list[dict[str, float]], name: str = "M1 constrained linear") -> LinearThermalModel:
    return fit_linear_model(rows, ["temp_c", "pwm", "load", "bias"], name=name, require_signs=True)


def fit_arx2(rows: list[dict[str, float]]) -> LinearThermalModel:
    enriched: list[dict[str, float]] = []
    for previous, current in zip(rows, rows[1:]):
        row = dict(current)
        row["temp_prev_c"] = previous["temp_c"]
        row["pwm_prev"] = previous["pwm"]
        row["load_prev"] = previous["load"]
        enriched.append(row)
    return fit_linear_model(
        enriched,
        ["temp_c", "temp_prev_c", "pwm", "pwm_prev", "load", "load_prev", "bias"],
        name="M2 second-order ARX",
        require_signs=True,
    )


def fit_rpm_model(rows: list[dict[str, float]]) -> LinearThermalModel:
    return fit_linear_model(
        rows,
        ["temp_c", "pwm", "rpm", "load", "bias"],
        name="M3 RPM model",
        require_signs=True,
    )


def fit_piecewise_linear(rows: list[dict[str, float]], split_temp_c: float = 60.0) -> PiecewiseLinearModel:
    low_rows = [row for row in rows if float(row["temp_c"]) < split_temp_c]
    high_rows = [row for row in rows if float(row["temp_c"]) >= split_temp_c]
    if len(low_rows) < 8 or len(high_rows) < 8:
        raise ValueError("piecewise model needs at least eight rows in each temperature segment")
    return PiecewiseLinearModel(
        name="M4 piecewise linear",
        split_temp_c=split_temp_c,
        low_model=fit_constrained_linear(low_rows, name="M4 low segment"),
        high_model=fit_constrained_linear(high_rows, name="M4 high segment"),
    )


def fit_linear_model(
    rows: list[dict[str, float]],
    feature_names: list[str],
    name: str,
    require_signs: bool,
) -> LinearThermalModel:
    if len(rows) < len(feature_names):
        raise ValueError(f"{name} needs at least {len(feature_names)} rows")
    matrix = [[0.0 for _ in feature_names] for _ in feature_names]
    vector = [0.0 for _ in feature_names]
    for row in rows:
        features = [float(row[feature]) if feature != "bias" else 1.0 for feature in feature_names]
        target = float(row["next_temp_c"])
        for i, value_i in enumerate(features):
            vector[i] += value_i * target
            for j, value_j in enumerate(features):
                matrix[i][j] += value_i * value_j
    coefficients = solve_linear_system(matrix, vector)
    model = LinearThermalModel(name=name, coefficients=coefficients, feature_names=feature_names)
    if require_signs:
        validate_signs(model)
    return model


def validate_signs(model: PredictiveModel) -> None:
    if model.pwm_coefficient is None or model.pwm_coefficient >= 0:
        raise ValueError(f"{model.name} failed PWM cooling sign check")
    if model.load_coefficient is None or model.load_coefficient <= 0:
        raise ValueError(f"{model.name} failed load heating sign check")


def solve_linear_system(matrix: list[list[float]], vector: list[float]) -> list[float]:
    size = len(vector)
    augmented = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda row: abs(augmented[row][col]))
        if abs(augmented[pivot][col]) < 1e-10:
            raise ValueError("linear fit is singular; collect more varied data")
        augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        scale = augmented[col][col]
        for j in range(col, size + 1):
            augmented[col][j] /= scale
        for row in range(size):
            if row == col:
                continue
            factor = augmented[row][col]
            for j in range(col, size + 1):
                augmented[row][j] -= factor * augmented[col][j]
    return [augmented[i][size] for i in range(size)]


def metrics(errors: list[float]) -> dict[str, float | int | None]:
    if not errors:
        return {"count": 0, "mae_c": None, "rmse_c": None, "bias_c": None}
    return {
        "count": len(errors),
        "mae_c": statistics.fmean(abs(error) for error in errors),
        "rmse_c": math.sqrt(statistics.fmean(error * error for error in errors)),
        "bias_c": statistics.fmean(errors),
    }


def evaluate_model(model: PredictiveModel, samples: list[dict[str, float]], rollout_steps: int = 12) -> dict[str, Any]:
    validate_signs(model)
    one_step_errors: list[float] = []
    one_step_high_errors: list[float] = []
    for index in range(len(samples) - 1):
        predicted = model.predict_next(samples, index, float(samples[index]["temp_c"]))
        actual = float(samples[index + 1]["temp_c"])
        error = predicted - actual
        one_step_errors.append(error)
        if actual > 60.0:
            one_step_high_errors.append(error)

    rollout_errors: list[float] = []
    rollout_high_errors: list[float] = []
    for start in range(0, max(0, len(samples) - rollout_steps)):
        predicted_temp = float(samples[start]["temp_c"])
        for offset in range(rollout_steps):
            index = start + offset
            predicted_temp = model.predict_next(samples, index, predicted_temp)
        actual = float(samples[start + rollout_steps]["temp_c"])
        error = predicted_temp - actual
        rollout_errors.append(error)
        if actual > 60.0:
            rollout_high_errors.append(error)

    high_bias = metrics(one_step_high_errors)["bias_c"]
    rollout_high_bias = metrics(rollout_high_errors)["bias_c"]
    return {
        "name": model.name,
        "valid": True,
        "parameters": model.parameters(),
        "constraints": {
            "pwm_coefficient": model.pwm_coefficient,
            "load_coefficient": model.load_coefficient,
            "pwm_coefficient_negative": (model.pwm_coefficient or 0.0) < 0,
            "load_coefficient_positive": (model.load_coefficient or 0.0) > 0,
            "high_temp_not_systematically_underestimated": high_bias is None or high_bias >= -0.5,
            "rollout_high_temp_not_systematically_underestimated": rollout_high_bias is None or rollout_high_bias >= -0.5,
        },
        "one_step": metrics(one_step_errors),
        "one_step_high_temp": metrics(one_step_high_errors),
        f"rollout_{rollout_steps}": metrics(rollout_errors),
        f"rollout_{rollout_steps}_high_temp": metrics(rollout_high_errors),
    }


def read_samples_csv(path: str) -> list[dict[str, float]]:
    import csv

    samples: list[dict[str, float]] = []
    with open(path, "r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            samples.append(
                {
                    "timestamp": float(row["timestamp"]),
                    "elapsed_s": float(row.get("elapsed_s", 0.0)),
                    "phase_index": float(row.get("phase_index", 0.0)),
                    "scheduled_pwm": float(row.get("scheduled_pwm", row.get("pwm", 0.0))),
                    "pwm": float(row["pwm"]),
                    "rpm": float(row["rpm"]),
                    "load": float(row["load"]),
                    "freq_mhz": float(row["freq_mhz"]),
                    "temp_c": float(row["temp_c"]),
                }
            )
    return samples


def fit_all_models(samples: list[dict[str, float]]) -> list[PredictiveModel | dict[str, Any]]:
    rows = paired_samples(samples)
    models: list[PredictiveModel | dict[str, Any]] = [DefaultModelAdapter(ThermalModel.default())]
    for name, fitter in (
        ("M1 constrained linear", fit_constrained_linear),
        ("M2 second-order ARX", fit_arx2),
        ("M3 RPM model", fit_rpm_model),
        ("M4 piecewise linear", fit_piecewise_linear),
    ):
        try:
            models.append(fitter(rows))
        except Exception as exc:  # noqa: BLE001
            models.append({"name": name, "valid": False, "error": str(exc)})
    return models


def compare_models(samples: list[dict[str, float]], rollout_steps: int = 12) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for model in fit_all_models(samples):
        if isinstance(model, dict):
            results.append(model)
            continue
        try:
            results.append(evaluate_model(model, samples, rollout_steps=rollout_steps))
        except Exception as exc:  # noqa: BLE001
            results.append({"name": model.name, "valid": False, "error": str(exc), "parameters": model.parameters()})
    return results
