import unittest

from ab_mpc_test import (
    choose_precondition_action,
    compare_arm_fairness,
    count_pwm_changes,
    count_pwm_reversals,
    official_step_pwm,
    summarize_samples,
)


class AbMpcTestTests(unittest.TestCase):
    def test_pwm_change_and_reversal_counts(self):
        pwms = [95, 95, 115, 115, 95, 75, 95]

        self.assertEqual(count_pwm_changes(pwms), 4)
        self.assertEqual(count_pwm_reversals(pwms), 2)

    def test_summarize_samples_reports_zone_and_prediction_metrics(self):
        samples = [
            {
                "elapsed_s": 0.0,
                "phase_index": 1,
                "temp_c": 57.0,
                "load": 0.25,
                "pwm": 95,
                "rpm": 3100,
                "predicted_max_temp_c": 58.5,
                "terminal_temp_c": 57.4,
            },
            {
                "elapsed_s": 2.0,
                "phase_index": 1,
                "temp_c": 58.5,
                "load": 0.75,
                "pwm": 115,
                "rpm": 3600,
                "predicted_max_temp_c": 59.0,
                "terminal_temp_c": 57.8,
            },
            {
                "elapsed_s": 4.0,
                "phase_index": 2,
                "temp_c": 57.5,
                "load": 0.50,
                "pwm": 115,
                "rpm": 3550,
                "predicted_max_temp_c": 58.0,
                "terminal_temp_c": 56.9,
            },
        ]

        summary = summarize_samples(samples, zone_high_c=58.0)

        self.assertEqual(summary["sample_count"], 3)
        self.assertEqual(summary["temp_peak_c"], 58.5)
        self.assertEqual(summary["over_zone_seconds"], 2.0)
        self.assertAlmostEqual(summary["over_zone_percent"], 50.0)
        self.assertAlmostEqual(summary["pwm_mean"], 325 / 3)
        self.assertEqual(summary["pwm_change_count"], 1)
        self.assertEqual(summary["predicted_max_peak_c"], 59.0)
        self.assertEqual(summary["terminal_mean_c"], (57.4 + 57.8 + 56.9) / 3)
        self.assertAlmostEqual(summary["load_mean"], 0.5)
        self.assertEqual(summary["phase_load_mean"], {"1": 0.5, "2": 0.5})

    def test_compare_arm_fairness_reports_start_and_load_balance(self):
        constant = {
            "temp_start_c": 57.0,
            "load_mean": 0.80,
            "phase_load_mean": {"1": 0.20, "2": 0.90},
        }
        segmented = {
            "temp_start_c": 57.2,
            "load_mean": 0.76,
            "phase_load_mean": {"1": 0.30, "2": 0.80},
        }

        fairness = compare_arm_fairness(constant, segmented)

        self.assertAlmostEqual(fairness["start_temp_delta_abs_c"], 0.2)
        self.assertAlmostEqual(fairness["load_mean_delta_abs"], 0.04)
        self.assertAlmostEqual(fairness["phase_load_rmse"], 0.1)
        self.assertEqual(fairness["common_phase_count"], 2)

    def test_cool_only_precondition_never_adds_cpu_heat(self):
        self.assertEqual(choose_precondition_action(58.5, 57.3, 0.6, "cool-only"), "cool")
        self.assertEqual(choose_precondition_action(57.3, 57.3, 0.6, "cool-only"), "hold")
        self.assertEqual(choose_precondition_action(56.2, 57.3, 0.6, "cool-only"), "wait")

    def test_warm_cool_precondition_can_add_heat_below_window(self):
        self.assertEqual(choose_precondition_action(56.2, 57.3, 0.6, "warm-cool"), "warm")

    def test_official_step_pwm_matches_config_thresholds(self):
        self.assertEqual(official_step_pwm(39.5, 0), 0)
        self.assertEqual(official_step_pwm(40.0, 0), 128)
        self.assertEqual(official_step_pwm(50.0, 128), 192)
        self.assertEqual(official_step_pwm(60.0, 192), 225)
        self.assertEqual(official_step_pwm(65.0, 225), 255)

    def test_official_step_pwm_uses_hysteresis_before_ramping_down(self):
        self.assertEqual(official_step_pwm(58.5, 225), 225)
        self.assertEqual(official_step_pwm(54.5, 225), 192)
        self.assertEqual(official_step_pwm(46.0, 192), 192)
        self.assertEqual(official_step_pwm(44.5, 192), 128)
        self.assertEqual(official_step_pwm(39.0, 128), 128)
        self.assertEqual(official_step_pwm(37.5, 128), 0)

    def test_official_step_pwm_can_scale_pwm_ladder_down(self):
        self.assertEqual(official_step_pwm(50.0, 0, pwm_scale=0.75), 144)
        self.assertEqual(official_step_pwm(60.0, 144, pwm_scale=0.75), 169)
        self.assertEqual(official_step_pwm(65.0, 169, pwm_scale=0.75), 191)
        self.assertEqual(official_step_pwm(54.5, 169, pwm_scale=0.75), 144)


if __name__ == "__main__":
    unittest.main()
