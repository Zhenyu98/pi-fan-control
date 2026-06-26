from __future__ import annotations

import csv
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Protocol, Sequence


class ThermalPredictor(Protocol):
    def predict(self, temp_c: float, pwm: float, load: float) -> float:
        ...

    def predict_with_state(
        self,
        temp_c: float,
        pwm: float,
        load: float,
        prev_temp_c: float,
        prev_pwm: float,
        prev_load: float,
    ) -> float:
        ...

    @property
    def pwm_coefficient(self) -> float:
        ...

    @property
    def load_coefficient(self) -> float:
        ...


@dataclass(frozen=True)
class ThermalModel:
    a: float
    b: float
    c: float
    d: float

    @classmethod
    def default(cls) -> "ThermalModel":
        return cls(a=0.97, b=-0.020, c=2.0, d=1.5)

    @classmethod
    def load(cls, path: str | Path) -> "ThermalModel":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(a=float(data["a"]), b=float(data["b"]), c=float(data["c"]), d=float(data["d"]))

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")

    def predict(self, temp_c: float, pwm: float, load: float) -> float:
        return self.a * temp_c + self.b * pwm + self.c * load + self.d

    def predict_with_state(
        self,
        temp_c: float,
        pwm: float,
        load: float,
        prev_temp_c: float,
        prev_pwm: float,
        prev_load: float,
    ) -> float:
        _ = (prev_temp_c, prev_pwm, prev_load)
        return self.predict(temp_c, pwm, load)

    @property
    def pwm_coefficient(self) -> float:
        return self.b

    @property
    def load_coefficient(self) -> float:
        return self.c


@dataclass(frozen=True)
class Arx2ThermalModel:
    temp_c: float
    temp_prev_c: float
    pwm: float
    pwm_prev: float
    load: float
    load_prev: float
    bias: float
    schema: str = "arx2"

    @classmethod
    def from_coefficients(cls, coefficients: Sequence[float]) -> "Arx2ThermalModel":
        if len(coefficients) != 7:
            raise ValueError("arx2 model requires seven coefficients")
        return cls(
            temp_c=float(coefficients[0]),
            temp_prev_c=float(coefficients[1]),
            pwm=float(coefficients[2]),
            pwm_prev=float(coefficients[3]),
            load=float(coefficients[4]),
            load_prev=float(coefficients[5]),
            bias=float(coefficients[6]),
        )

    @classmethod
    def m2_identified(cls) -> "Arx2ThermalModel":
        return cls.from_coefficients(
            [
                0.5567670422114871,
                0.4184214650812279,
                -0.0014623726815126798,
                0.000059901610051346036,
                0.681352157701699,
                -0.15616360945374425,
                1.2924654171170944,
            ]
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "feature_names": ["temp_c", "temp_prev_c", "pwm", "pwm_prev", "load", "load_prev", "bias"],
            "coefficients": [
                self.temp_c,
                self.temp_prev_c,
                self.pwm,
                self.pwm_prev,
                self.load,
                self.load_prev,
                self.bias,
            ],
            "pwm_coefficient": self.pwm_coefficient,
            "load_coefficient": self.load_coefficient,
            "source": "identification/20260626-105237 M2 second-order ARX",
        }

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")

    def predict(self, temp_c: float, pwm: float, load: float) -> float:
        return self.predict_with_state(temp_c, pwm, load, temp_c, pwm, load)

    def predict_with_state(
        self,
        temp_c: float,
        pwm: float,
        load: float,
        prev_temp_c: float,
        prev_pwm: float,
        prev_load: float,
    ) -> float:
        return (
            self.temp_c * temp_c
            + self.temp_prev_c * prev_temp_c
            + self.pwm * pwm
            + self.pwm_prev * prev_pwm
            + self.load * load
            + self.load_prev * prev_load
            + self.bias
        )

    @property
    def pwm_coefficient(self) -> float:
        return self.pwm + self.pwm_prev

    @property
    def load_coefficient(self) -> float:
        return self.load + self.load_prev


def load_predictor(path: str | Path) -> ThermalPredictor:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    schema = data.get("schema")
    if schema == "arx2":
        return Arx2ThermalModel.from_coefficients(data["coefficients"])
    if schema in (None, "first_order"):
        return ThermalModel(a=float(data["a"]), b=float(data["b"]), c=float(data["c"]), d=float(data["d"]))
    raise ValueError(f"unsupported model schema: {schema}")


@dataclass(frozen=True)
class Sample:
    timestamp: float
    temp_c: float
    pwm: int
    rpm: int
    load: float
    freq_mhz: float


def clamp(value: float, minimum: float, maximum: float) -> float:
    if math.isnan(value):
        raise ValueError("cannot clamp NaN")
    return max(minimum, min(maximum, value))


@dataclass(frozen=True)
class PredictionStats:
    pred_error_c: float | None = None
    bias_ewma_c: float = 0.0
    rmse_ewma_c: float = 0.0
    prediction_margin_c: float = 0.0


@dataclass
class PredictionObserver:
    alpha: float = 0.08
    max_margin_c: float = 3.0
    bias_ewma_c: float = 0.0
    mse_ewma_c2: float = 0.0
    last_prediction_c: float | None = None

    def __post_init__(self) -> None:
        if not 0.0 < self.alpha <= 1.0:
            raise ValueError("alpha must be in (0, 1]")
        if self.max_margin_c < 0:
            raise ValueError("max_margin_c must not be negative")

    def observe(self, actual_temp_c: float) -> PredictionStats:
        if self.last_prediction_c is None:
            return self.stats()

        pred_error = actual_temp_c - self.last_prediction_c
        self.bias_ewma_c = (1.0 - self.alpha) * self.bias_ewma_c + self.alpha * pred_error
        self.mse_ewma_c2 = (1.0 - self.alpha) * self.mse_ewma_c2 + self.alpha * pred_error * pred_error
        return self.stats(pred_error)

    def record_prediction(self, predicted_temp_c: float) -> None:
        self.last_prediction_c = predicted_temp_c

    def stats(self, pred_error_c: float | None = None) -> PredictionStats:
        rmse = math.sqrt(max(0.0, self.mse_ewma_c2))
        margin = clamp(max(0.0, self.bias_ewma_c) + 0.5 * rmse, 0.0, self.max_margin_c)
        return PredictionStats(
            pred_error_c=pred_error_c,
            bias_ewma_c=self.bias_ewma_c,
            rmse_ewma_c=rmse,
            prediction_margin_c=margin,
        )


def _solve_linear_system(matrix: list[list[float]], vector: list[float]) -> list[float]:
    size = len(vector)
    augmented = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda row: abs(augmented[row][col]))
        if abs(augmented[pivot][col]) < 1e-12:
            raise ValueError("model fit is singular; collect more varied data")
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


def fit_thermal_model(rows: Iterable[dict[str, float]]) -> ThermalModel:
    xtx = [[0.0 for _ in range(4)] for _ in range(4)]
    xty = [0.0 for _ in range(4)]
    count = 0

    for row in rows:
        features = [
            float(row["temp_c"]),
            float(row["pwm"]),
            float(row["load"]),
            1.0,
        ]
        target = float(row["next_temp_c"])
        for i in range(4):
            xty[i] += features[i] * target
            for j in range(4):
                xtx[i][j] += features[i] * features[j]
        count += 1

    if count < 4:
        raise ValueError("need at least four paired samples to fit the thermal model")

    a, b, c, d = _solve_linear_system(xtx, xty)
    if b >= 0:
        raise ValueError("fitted PWM coefficient is not cooling; collect better data")
    return ThermalModel(a=a, b=b, c=c, d=d)


def arx2_training_rows(samples: Sequence[Sample]) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    for previous, current, following in zip(samples, samples[1:], samples[2:]):
        rows.append(
            {
                "temp_c": current.temp_c,
                "temp_prev_c": previous.temp_c,
                "pwm": float(current.pwm),
                "pwm_prev": float(previous.pwm),
                "load": current.load,
                "load_prev": previous.load,
                "next_temp_c": following.temp_c,
            }
        )
    return rows


def fit_arx2_thermal_model(samples: Sequence[Sample]) -> Arx2ThermalModel:
    rows = arx2_training_rows(samples)
    if len(rows) < 7:
        raise ValueError("need at least nine ordered samples to fit the ARX2 thermal model")

    xtx = [[0.0 for _ in range(7)] for _ in range(7)]
    xty = [0.0 for _ in range(7)]
    for row in rows:
        features = [
            float(row["temp_c"]),
            float(row["temp_prev_c"]),
            float(row["pwm"]),
            float(row["pwm_prev"]),
            float(row["load"]),
            float(row["load_prev"]),
            1.0,
        ]
        target = float(row["next_temp_c"])
        for i in range(7):
            xty[i] += features[i] * target
            for j in range(7):
                xtx[i][j] += features[i] * features[j]

    candidate = Arx2ThermalModel.from_coefficients(_solve_linear_system(xtx, xty))
    if candidate.pwm_coefficient >= 0:
        raise ValueError("fitted PWM coefficient is not cooling; collect better data")
    if candidate.load_coefficient <= 0:
        raise ValueError("fitted load coefficient is not heating; collect better data")
    return candidate


@dataclass
class ZoneMpcDecision:
    pwm: int
    predicted_max_temp_c: float
    terminal_temp_c: float
    violation_steps: int
    violation_area_c_steps: float
    reason: str
    cost: float


@dataclass
class ZoneMpcController:
    model: ThermalPredictor
    zone_low_temp_c: float = 50.0
    zone_high_temp_c: float = 60.0
    full_temp_c: float = 69.0
    min_pwm: int = 0
    max_pwm: int = 255
    max_pwm_step: int = 20
    min_active_pwm: int = 75
    idle_stop_temp_c: float = 50.0
    safety_temp_c: float = 70.0
    safety_pwm: int = 255
    horizon_steps: int = 12
    candidate_pwm_step: int = 5
    over_temp_weight: float = 160.0
    under_temp_weight: float = 0.1
    pwm_weight: float = 0.35
    step_weight: float = 0.25
    full_temp_weight: float = 500.0
    sustained_violation_weight: float = 35.0
    terminal_violation_weight: float = 120.0
    max_temp_62_weight: float = 240.0
    max_temp_65_weight: float = 900.0

    def __post_init__(self) -> None:
        if self.zone_low_temp_c >= self.zone_high_temp_c:
            raise ValueError("zone_low_temp_c must be below zone_high_temp_c")
        if self.zone_high_temp_c > self.full_temp_c:
            raise ValueError("zone_high_temp_c must be no higher than full_temp_c")
        if self.full_temp_c > self.safety_temp_c:
            raise ValueError("full_temp_c must be no higher than safety_temp_c")
        if self.horizon_steps < 1:
            raise ValueError("horizon_steps must be positive")
        if self.candidate_pwm_step < 1:
            raise ValueError("candidate_pwm_step must be positive")
        if self.max_pwm_step < 1:
            raise ValueError("max_pwm_step must be positive")

    def next_pwm(self, temp_c: float, load: float, current_pwm: int) -> int:
        return self.decide(temp_c=temp_c, load=load, current_pwm=current_pwm).pwm

    def decide(
        self,
        temp_c: float,
        load: float,
        current_pwm: int,
        prediction_margin_c: float = 0.0,
        prev_temp_c: float | None = None,
        prev_pwm: int | None = None,
        prev_load: float | None = None,
    ) -> ZoneMpcDecision:
        if temp_c >= self.safety_temp_c:
            pwm = int(clamp(self.safety_pwm, self.min_pwm, self.max_pwm))
            return ZoneMpcDecision(
                pwm=pwm,
                predicted_max_temp_c=temp_c,
                terminal_temp_c=temp_c,
                violation_steps=1 if temp_c > self.zone_high_temp_c else 0,
                violation_area_c_steps=max(0.0, temp_c - self.zone_high_temp_c),
                reason="safety_temp",
                cost=0.0,
            )
        if temp_c >= self.full_temp_c:
            return ZoneMpcDecision(
                pwm=self.max_pwm,
                predicted_max_temp_c=temp_c,
                terminal_temp_c=temp_c,
                violation_steps=1 if temp_c > self.zone_high_temp_c else 0,
                violation_area_c_steps=max(0.0, temp_c - self.zone_high_temp_c),
                reason="full_temp",
                cost=0.0,
            )
        if temp_c <= self.idle_stop_temp_c and current_pwm <= self.min_active_pwm:
            return ZoneMpcDecision(
                pwm=0,
                predicted_max_temp_c=temp_c,
                terminal_temp_c=temp_c,
                violation_steps=0,
                violation_area_c_steps=0.0,
                reason="idle_stop",
                cost=0.0,
            )

        load = clamp(load, 0.0, 1.0)
        margin = clamp(prediction_margin_c, 0.0, 3.0)
        candidates = self._candidate_pwms(current_pwm)
        return min(
            (
                self._trajectory_decision(
                    temp_c=temp_c,
                    load=load,
                    pwm=candidate,
                    current_pwm=current_pwm,
                    prediction_margin_c=margin,
                    prev_temp_c=temp_c if prev_temp_c is None else prev_temp_c,
                    prev_pwm=current_pwm if prev_pwm is None else prev_pwm,
                    prev_load=load if prev_load is None else prev_load,
                )
                for candidate in candidates
            ),
            key=lambda decision: (decision.cost, decision.pwm),
        )

    def _candidate_pwms(self, current_pwm: int) -> list[int]:
        desired_values = {0, current_pwm, self.min_active_pwm, self.max_pwm}
        desired_values.update(range(self.min_active_pwm, self.max_pwm + 1, self.candidate_pwm_step))
        return sorted({self._limit_pwm_step(desired, current_pwm) for desired in desired_values})

    def _limit_pwm_step(self, desired_pwm: int, current_pwm: int) -> int:
        desired = int(round(clamp(float(desired_pwm), self.min_pwm, self.max_pwm)))
        current = int(round(clamp(float(current_pwm), self.min_pwm, self.max_pwm)))
        if 0 < desired < self.min_active_pwm:
            desired = self.min_active_pwm

        low = max(self.min_pwm, current - self.max_pwm_step)
        high = min(self.max_pwm, current + self.max_pwm_step)
        limited = int(clamp(float(desired), low, high))
        if 0 < limited < self.min_active_pwm:
            return self.min_active_pwm if desired > 0 else 0
        return limited

    def _trajectory_decision(
        self,
        temp_c: float,
        load: float,
        pwm: int,
        current_pwm: int,
        prediction_margin_c: float,
        prev_temp_c: float,
        prev_pwm: int,
        prev_load: float,
    ) -> ZoneMpcDecision:
        temp = temp_c
        previous_temp = prev_temp_c
        previous_pwm = float(prev_pwm)
        previous_load = prev_load
        cost = self.step_weight * ((pwm - current_pwm) / max(1.0, float(self.max_pwm_step))) ** 2
        normalized_pwm = pwm / max(1.0, float(self.max_pwm))
        predicted_max = temp_c
        violation_steps = 0
        violation_area = 0.0

        for _ in range(self.horizon_steps):
            next_temp = self.model.predict_with_state(
                temp_c=temp,
                pwm=pwm,
                load=load,
                prev_temp_c=previous_temp,
                prev_pwm=previous_pwm,
                prev_load=previous_load,
            )
            previous_temp = temp
            previous_pwm = float(pwm)
            previous_load = load
            temp = next_temp
            safe_temp = temp + prediction_margin_c
            predicted_max = max(predicted_max, safe_temp)
            above = max(0.0, safe_temp - self.zone_high_temp_c)
            below = max(0.0, self.zone_low_temp_c - safe_temp)
            full_excess = max(0.0, safe_temp - self.full_temp_c)
            if above > 0:
                violation_steps += 1
                violation_area += above
            cost += self.over_temp_weight * above * above
            cost += self.under_temp_weight * below * below
            cost += self.full_temp_weight * full_excess * full_excess
            cost += self.pwm_weight * normalized_pwm * normalized_pwm

        terminal_temp = temp + prediction_margin_c
        terminal_violation = max(0.0, terminal_temp - self.zone_high_temp_c)
        max_over_62 = max(0.0, predicted_max - 62.0)
        max_over_65 = max(0.0, predicted_max - 65.0)
        sustained_steps = max(0, violation_steps - 2)
        cost += self.sustained_violation_weight * sustained_steps * sustained_steps
        cost += self.terminal_violation_weight * terminal_violation * terminal_violation
        cost += self.max_temp_62_weight * max_over_62 * max_over_62
        cost += self.max_temp_65_weight * max_over_65 * max_over_65

        return ZoneMpcDecision(
            pwm=pwm,
            predicted_max_temp_c=predicted_max,
            terminal_temp_c=terminal_temp,
            violation_steps=violation_steps,
            violation_area_c_steps=violation_area,
            reason=self._reason(predicted_max, terminal_temp, violation_steps, violation_area),
            cost=cost,
        )

    def _trajectory_cost(self, temp_c: float, load: float, pwm: int, current_pwm: int) -> float:
        return self._trajectory_decision(
            temp_c=temp_c,
            load=load,
            pwm=pwm,
            current_pwm=current_pwm,
            prediction_margin_c=0.0,
            prev_temp_c=temp_c,
            prev_pwm=current_pwm,
            prev_load=load,
        ).cost

    def _reason(
        self,
        predicted_max_temp_c: float,
        terminal_temp_c: float,
        violation_steps: int,
        violation_area_c_steps: float,
    ) -> str:
        reasons: list[str] = []
        if violation_steps == 0:
            reasons.append("inside_zone")
        elif violation_steps <= 2 and violation_area_c_steps <= 1.0:
            reasons.append("brief_violation")
        else:
            reasons.append("sustained_violation")
        if terminal_temp_c > self.zone_high_temp_c:
            reasons.append("terminal_violation")
        if predicted_max_temp_c > 65.0:
            reasons.append("max_over_65")
        elif predicted_max_temp_c > 62.0:
            reasons.append("max_over_62")
        return "+".join(reasons)


def read_csv_samples(path: str | Path) -> list[Sample]:
    samples: list[Sample] = []
    with Path(path).open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            samples.append(
                Sample(
                    timestamp=float(row["timestamp"]),
                    temp_c=float(row["temp_c"]),
                    pwm=int(float(row["pwm"])),
                    rpm=int(float(row["rpm"])),
                    load=float(row["load"]),
                    freq_mhz=float(row["freq_mhz"]),
                )
            )
    return samples


def paired_rows(samples: Sequence[Sample]) -> list[dict[str, float]]:
    return [
        {
            "temp_c": current.temp_c,
            "pwm": float(current.pwm),
            "load": current.load,
            "next_temp_c": following.temp_c,
        }
        for current, following in zip(samples, samples[1:])
    ]
