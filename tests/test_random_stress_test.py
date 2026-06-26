import unittest

from fan_control_core import ThermalModel
from random_stress_test import build_random_schedule, prediction_summary


class RandomStressTestTests(unittest.TestCase):
    def test_build_random_schedule_is_deterministic_and_covers_duration(self):
        first = build_random_schedule(total_duration_s=120, min_phase_s=20, max_phase_s=40, max_workers=4, seed=7)
        second = build_random_schedule(total_duration_s=120, min_phase_s=20, max_phase_s=40, max_workers=4, seed=7)

        self.assertEqual(first, second)
        self.assertEqual(sum(phase.duration_s for phase in first), 120)
        self.assertTrue(all(0 <= phase.cpu_workers <= 4 for phase in first))

    def test_prediction_summary_reports_one_step_error(self):
        model = ThermalModel(a=1.0, b=-0.1, c=2.0, d=0.0)
        samples = [
            {"temp_c": 50.0, "pwm": 0, "load": 1.0},
            {"temp_c": 52.0, "pwm": 10, "load": 1.0},
            {"temp_c": 53.0, "pwm": 10, "load": 0.0},
        ]

        summary = prediction_summary(samples, model)

        self.assertEqual(summary["paired_samples"], 2)
        self.assertAlmostEqual(summary["mae_c"], 0.0)
        self.assertAlmostEqual(summary["rmse_c"], 0.0)
        self.assertAlmostEqual(summary["bias_c"], 0.0)


if __name__ == "__main__":
    unittest.main()
