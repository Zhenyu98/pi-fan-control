import unittest

from model_identification import (
    ExperimentPhase,
    LinearThermalModel,
    build_identification_schedule,
    evaluate_model,
    fit_constrained_linear,
)


class ModelIdentificationTests(unittest.TestCase):
    def test_identification_schedule_covers_load_pwm_grid_and_has_cooling_stage(self):
        schedule = build_identification_schedule(phase_duration_s=180)

        pairs = {(phase.load_name, phase.scheduled_pwm) for phase in schedule}

        self.assertEqual(len(schedule), 21)
        self.assertEqual(
            pairs,
            {
                (load, pwm)
                for load in ("idle", "cpu2", "cpu4")
                for pwm in (0, 75, 100, 130, 160, 200, 255)
            },
        )
        self.assertEqual([phase.scheduled_pwm for phase in schedule if phase.load_name == "cpu4"], [255, 200, 160, 130, 100, 75, 0])

    def test_constrained_linear_fit_recovers_signs(self):
        samples = []
        for temp in (50.0, 55.0, 60.0, 65.0):
            for pwm in (0.0, 100.0, 200.0):
                for load in (0.0, 0.5, 1.0):
                    samples.append({"temp_c": temp, "pwm": pwm, "load": load, "rpm": pwm * 30.0, "freq_mhz": 2400.0})
                    next_temp = 0.96 * temp - 0.018 * pwm + 1.8 * load + 2.0
                    samples[-1]["next_temp_c"] = next_temp

        model = fit_constrained_linear(samples)

        self.assertLess(model.pwm_coefficient, 0.0)
        self.assertGreater(model.load_coefficient, 0.0)

    def test_evaluate_model_reports_one_and_twelve_step_metrics(self):
        samples = [
            ExperimentPhase.sample_dict(temp_c=50.0 + index, pwm=0, load=0.0, rpm=0, freq_mhz=1600.0)
            for index in range(20)
        ]
        model = LinearThermalModel(
            name="test",
            coefficients=[1.0, -0.01, 0.1, 1.0],
            feature_names=["temp_c", "pwm", "load", "bias"],
        )

        result = evaluate_model(model, samples, rollout_steps=12)

        self.assertEqual(result["one_step"]["count"], 19)
        self.assertEqual(result["rollout_12"]["count"], 8)
        self.assertAlmostEqual(result["one_step"]["mae_c"], 0.0)
        self.assertAlmostEqual(result["rollout_12"]["mae_c"], 0.0)


if __name__ == "__main__":
    unittest.main()
