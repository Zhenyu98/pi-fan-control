import tempfile
import unittest
from pathlib import Path

from fan_control_core import Arx2ThermalModel, Sample, ThermalModel, load_predictor
from fan_control_shadow import (
    ShadowConfig,
    ShadowLearner,
    evaluate_candidate_model,
    promote_model_atomically,
)


def make_samples(model: ThermalModel, count: int = 36) -> list[Sample]:
    temp = 52.0
    samples: list[Sample] = []
    for index in range(count):
        pwm = 60 + (index % 6) * 28
        load = 0.15 + (index % 5) * 0.17
        samples.append(
            Sample(
                timestamp=float(index),
                temp_c=temp,
                pwm=pwm,
                rpm=1200 + pwm * 22,
                load=load,
                freq_mhz=2400.0,
            )
        )
        temp = model.predict(temp, pwm, load)
    return samples


def make_arx_samples(model: Arx2ThermalModel, count: int = 90) -> list[Sample]:
    temp = 52.0
    prev_temp = 51.5
    prev_pwm = 75
    prev_load = 0.1
    samples: list[Sample] = []
    pwm_values = [0, 75, 100, 130, 160, 200, 255]
    load_values = [0.0, 0.2, 0.45, 0.7, 1.0]
    for index in range(count):
        pwm = pwm_values[(index * 3) % len(pwm_values)]
        load = load_values[(index * 2) % len(load_values)]
        samples.append(
            Sample(
                timestamp=float(index),
                temp_c=temp,
                pwm=pwm,
                rpm=0 if pwm == 0 else 1300 + pwm * 23,
                load=load,
                freq_mhz=2400.0,
            )
        )
        next_temp = model.predict_with_state(
            temp_c=temp,
            pwm=pwm,
            load=load,
            prev_temp_c=prev_temp,
            prev_pwm=prev_pwm,
            prev_load=prev_load,
        )
        prev_temp = temp
        prev_pwm = pwm
        prev_load = load
        temp = next_temp
    return samples


class ShadowLearningTests(unittest.TestCase):
    def test_candidate_is_accepted_when_error_improves_and_safety_checks_pass(self):
        true_model = ThermalModel(a=0.94, b=-0.018, c=1.6, d=3.2)
        stale_model = ThermalModel(a=0.99, b=-0.004, c=0.2, d=0.1)
        config = ShadowConfig(
            min_samples=20,
            min_improvement=0.20,
            min_pwm_span=40,
            min_load_span=0.20,
            max_param_change=40.0,
        )

        decision = evaluate_candidate_model(stale_model, make_samples(true_model), config)

        self.assertTrue(decision.accepted, decision.reason)
        self.assertLess(decision.candidate_mae, decision.current_mae * 0.8)
        self.assertLess(decision.candidate.b, 0)

    def test_candidate_is_rejected_when_pwm_does_not_vary_enough(self):
        model = ThermalModel(a=0.94, b=-0.018, c=1.6, d=3.2)
        samples = [
            Sample(
                timestamp=float(index),
                temp_c=50.0 + index * 0.1,
                pwm=75,
                rpm=1800,
                load=0.2 + (index % 4) * 0.1,
                freq_mhz=2400.0,
            )
            for index in range(30)
        ]

        decision = evaluate_candidate_model(model, samples, ShadowConfig(min_samples=20, min_pwm_span=20))

        self.assertFalse(decision.accepted)
        self.assertIn("pwm span", decision.reason)

    def test_arx2_candidate_is_accepted_when_rollout_model_improves(self):
        true_model = Arx2ThermalModel(
            temp_c=0.56,
            temp_prev_c=0.41,
            pwm=-0.0015,
            pwm_prev=0.0001,
            load=0.68,
            load_prev=-0.16,
            bias=1.30,
        )
        stale_model = Arx2ThermalModel(
            temp_c=0.50,
            temp_prev_c=0.45,
            pwm=-0.0005,
            pwm_prev=0.0,
            load=0.20,
            load_prev=0.0,
            bias=2.20,
        )
        config = ShadowConfig(
            min_samples=40,
            min_improvement=0.20,
            min_pwm_span=40,
            min_load_span=0.20,
            max_param_change=40.0,
        )

        decision = evaluate_candidate_model(stale_model, make_arx_samples(true_model), config)

        self.assertTrue(decision.accepted, decision.reason)
        self.assertIsInstance(decision.candidate, Arx2ThermalModel)
        self.assertLess(decision.candidate_mae, decision.current_mae * 0.8)
        self.assertLess(decision.candidate.pwm_coefficient, 0.0)
        self.assertGreater(decision.candidate.load_coefficient, 0.0)

    def test_promote_model_atomically_writes_model_and_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            model_path = Path(tmp) / "model.json"
            old_model = ThermalModel(a=0.97, b=-0.020, c=2.0, d=1.5)
            new_model = ThermalModel(a=0.94, b=-0.018, c=1.6, d=3.2)
            old_model.save(model_path)

            promote_model_atomically(new_model, model_path)

            self.assertEqual(ThermalModel.load(model_path), new_model)
            self.assertEqual(ThermalModel.load(model_path.with_suffix(".previous.json")), old_model)

    def test_promote_model_atomically_writes_arx2_model_and_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            model_path = Path(tmp) / "model_arx2.json"
            old_model = Arx2ThermalModel.m2_identified()
            new_model = Arx2ThermalModel(
                temp_c=0.57,
                temp_prev_c=0.40,
                pwm=-0.0016,
                pwm_prev=0.0001,
                load=0.64,
                load_prev=-0.12,
                bias=1.20,
            )
            old_model.save(model_path)

            promote_model_atomically(new_model, model_path)

            self.assertEqual(load_predictor(model_path), new_model)
            self.assertEqual(load_predictor(model_path.with_suffix(".previous.json")), old_model)

    def test_repeated_promotions_keep_only_current_and_previous_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model_path = root / "model.json"
            ThermalModel(a=0.97, b=-0.020, c=2.0, d=1.5).save(model_path)

            promote_model_atomically(ThermalModel(a=0.94, b=-0.018, c=1.6, d=3.2), model_path)
            promote_model_atomically(ThermalModel(a=0.93, b=-0.017, c=1.5, d=3.0), model_path)

            self.assertEqual(sorted(path.name for path in root.glob("model*.json")), ["model.json", "model.previous.json"])

    def test_shadow_sample_log_rotates_to_single_previous_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "shadow_samples.csv"
            learner = ShadowLearner(
                ShadowConfig(
                    min_samples=3,
                    max_log_bytes=120,
                    log_path=log_path,
                )
            )
            sample = Sample(timestamp=1.0, temp_c=50.0, pwm=75, rpm=1800, load=0.5, freq_mhz=2400.0)

            for _ in range(8):
                learner.observe(sample)

            self.assertTrue(log_path.exists())
            self.assertTrue(log_path.with_suffix(".csv.1").exists())
            self.assertFalse(log_path.with_suffix(".csv.2").exists())


if __name__ == "__main__":
    unittest.main()
