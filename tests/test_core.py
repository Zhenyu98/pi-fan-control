import math
import unittest

from fan_control_core import (
    Arx2ThermalModel,
    PredictionObserver,
    Sample,
    ThermalModel,
    ZoneMpcController,
    clamp,
    fit_arx2_thermal_model,
    fit_thermal_model,
    load_predictor,
)


class CoreTests(unittest.TestCase):
    def test_fit_thermal_model_recovers_simple_coefficients(self):
        rows = []
        for temp in (45.0, 50.0, 55.0, 60.0):
            for pwm in (0.0, 80.0, 160.0):
                for load in (0.1, 0.7):
                    next_temp = 0.94 * temp - 0.018 * pwm + 1.7 * load + 3.0
                    rows.append({"temp_c": temp, "pwm": pwm, "load": load, "next_temp_c": next_temp})

        model = fit_thermal_model(rows)

        self.assertAlmostEqual(model.a, 0.94, places=6)
        self.assertAlmostEqual(model.b, -0.018, places=6)
        self.assertAlmostEqual(model.c, 1.7, places=6)
        self.assertAlmostEqual(model.d, 3.0, places=6)

    def test_clamp_rejects_nan(self):
        with self.assertRaises(ValueError):
            clamp(math.nan, 0, 255)

    def test_zone_mpc_keeps_fan_off_inside_safe_zone(self):
        model = ThermalModel(a=0.96, b=-0.025, c=0.8, d=1.2)
        controller = ZoneMpcController(
            model=model,
            zone_low_temp_c=50.0,
            zone_high_temp_c=60.0,
            full_temp_c=69.0,
            max_pwm_step=20,
            min_active_pwm=60,
        )

        self.assertEqual(controller.next_pwm(temp_c=55.0, load=0.0, current_pwm=0), 0)

    def test_zone_mpc_defaults_are_conservative_for_pi5_official_fan(self):
        controller = ZoneMpcController(model=ThermalModel.default())

        self.assertEqual(controller.min_active_pwm, 75)
        self.assertEqual(controller.max_pwm_step, 20)
        self.assertEqual(controller.safety_temp_c, 70.0)
        self.assertEqual(controller.pwm_weight, 1.2)
        self.assertEqual(controller.max_temp_62_weight, 160.0)

    def test_candidate_pwm_respects_max_step_when_ramping_down_to_zero(self):
        controller = ZoneMpcController(
            model=ThermalModel.default(),
            min_active_pwm=75,
            max_pwm_step=20,
        )

        candidates = controller._candidate_pwms(75)

        self.assertNotIn(0, candidates)
        self.assertTrue(all(candidate == 0 or candidate >= 75 for candidate in candidates))
        self.assertTrue(all(abs(candidate - 75) <= 20 for candidate in candidates))

    def test_zone_mpc_starts_fan_when_temperature_exceeds_zone(self):
        model = ThermalModel(a=0.96, b=-0.025, c=0.8, d=1.2)
        controller = ZoneMpcController(
            model=model,
            zone_low_temp_c=50.0,
            zone_high_temp_c=60.0,
            full_temp_c=69.0,
            max_pwm_step=20,
            min_active_pwm=75,
        )

        self.assertEqual(controller.next_pwm(temp_c=61.0, load=1.0, current_pwm=0), 75)

    def test_zone_mpc_increases_pwm_before_full_speed_threshold(self):
        model = ThermalModel(a=0.96, b=-0.025, c=0.8, d=1.2)
        controller = ZoneMpcController(
            model=model,
            zone_low_temp_c=50.0,
            zone_high_temp_c=60.0,
            full_temp_c=69.0,
            max_pwm_step=20,
            min_active_pwm=60,
        )

        self.assertGreater(controller.next_pwm(temp_c=66.0, load=1.0, current_pwm=80), 80)

    def test_zone_mpc_forces_full_speed_at_full_temperature(self):
        model = ThermalModel(a=0.96, b=-0.025, c=0.8, d=1.2)
        controller = ZoneMpcController(
            model=model,
            zone_low_temp_c=50.0,
            zone_high_temp_c=60.0,
            full_temp_c=69.0,
            safety_temp_c=72.0,
            safety_pwm=255,
        )

        self.assertEqual(controller.next_pwm(temp_c=69.0, load=1.0, current_pwm=80), 255)

    def test_prediction_observer_margin_only_increases_for_underprediction(self):
        observer = PredictionObserver(alpha=0.5)

        observer.record_prediction(60.0)
        stats = observer.observe(actual_temp_c=62.0)

        self.assertAlmostEqual(stats.pred_error_c, 2.0)
        self.assertAlmostEqual(stats.bias_ewma_c, 1.0)
        self.assertAlmostEqual(stats.rmse_ewma_c, math.sqrt(2.0))
        self.assertAlmostEqual(stats.prediction_margin_c, 1.0 + 0.5 * math.sqrt(2.0))

        observer.record_prediction(62.0)
        stats = observer.observe(actual_temp_c=60.0)

        self.assertAlmostEqual(stats.pred_error_c, -2.0)
        self.assertGreaterEqual(stats.prediction_margin_c, 0.0)

    def test_prediction_observer_clamps_margin_to_three_degrees(self):
        observer = PredictionObserver(alpha=1.0)

        observer.record_prediction(50.0)
        stats = observer.observe(actual_temp_c=60.0)

        self.assertEqual(stats.prediction_margin_c, 3.0)

    def test_zone_mpc_margin_can_only_raise_safe_predictions(self):
        model = ThermalModel(a=1.0, b=0.0, c=0.0, d=1.0)
        controller = ZoneMpcController(model=model, horizon_steps=3)

        without_margin = controller.decide(temp_c=58.0, load=0.0, current_pwm=0, prediction_margin_c=0.0)
        with_margin = controller.decide(temp_c=58.0, load=0.0, current_pwm=0, prediction_margin_c=2.0)

        self.assertGreaterEqual(with_margin.predicted_max_temp_c, without_margin.predicted_max_temp_c)
        self.assertGreaterEqual(with_margin.terminal_temp_c, without_margin.terminal_temp_c)

    def test_zone_mpc_reports_sustained_violation_diagnostics(self):
        model = ThermalModel(a=1.0, b=0.0, c=0.0, d=1.0)
        controller = ZoneMpcController(
            model=model,
            zone_high_temp_c=60.0,
            full_temp_c=69.0,
            safety_temp_c=70.0,
            horizon_steps=4,
        )

        decision = controller.decide(temp_c=60.0, load=0.0, current_pwm=0, prediction_margin_c=0.0)

        self.assertEqual(decision.violation_steps, 4)
        self.assertGreater(decision.violation_area_c_steps, 0.0)
        self.assertGreater(decision.terminal_temp_c, 60.0)
        self.assertIn("sustained_violation", decision.reason)

    def test_arx2_model_uses_previous_temperature_pwm_and_load(self):
        model = Arx2ThermalModel(
            temp_c=0.5,
            temp_prev_c=0.25,
            pwm=-0.01,
            pwm_prev=-0.005,
            load=2.0,
            load_prev=1.0,
            bias=3.0,
        )

        predicted = model.predict_with_state(
            temp_c=60.0,
            pwm=100.0,
            load=0.8,
            prev_temp_c=58.0,
            prev_pwm=80.0,
            prev_load=0.5,
        )

        self.assertAlmostEqual(predicted, 48.2)
        self.assertLess(model.pwm_coefficient, 0.0)
        self.assertGreater(model.load_coefficient, 0.0)

    def test_fit_arx2_thermal_model_recovers_second_order_coefficients(self):
        true_model = Arx2ThermalModel(
            temp_c=0.55,
            temp_prev_c=0.42,
            pwm=-0.0015,
            pwm_prev=0.0001,
            load=0.68,
            load_prev=-0.16,
            bias=1.29,
        )
        samples = []
        temp = 51.0
        prev_temp = 50.5
        prev_pwm = 75
        prev_load = 0.1
        for index in range(80):
            pwm = [0, 75, 100, 130, 160, 200, 255][index % 7]
            load = [0.0, 0.25, 0.5, 0.75, 1.0][(index * 2) % 5]
            samples.append(
                Sample(
                    timestamp=float(index),
                    temp_c=temp,
                    pwm=pwm,
                    rpm=0 if pwm == 0 else 1200 + pwm * 24,
                    load=load,
                    freq_mhz=2400.0,
                )
            )
            next_temp = true_model.predict_with_state(
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

        fitted = fit_arx2_thermal_model(samples)

        self.assertAlmostEqual(fitted.pwm_coefficient, true_model.pwm_coefficient, places=5)
        self.assertAlmostEqual(fitted.load_coefficient, true_model.load_coefficient, places=5)
        self.assertAlmostEqual(fitted.temp_c + fitted.temp_prev_c, true_model.temp_c + true_model.temp_prev_c, places=5)

    def test_load_predictor_supports_legacy_and_arx2_json(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            legacy = Path(tmpdir) / "legacy.json"
            legacy.write_text('{"a": 0.9, "b": -0.01, "c": 1.0, "d": 2.0}\n', encoding="utf-8")
            self.assertIsInstance(load_predictor(legacy), ThermalModel)

            arx = Path(tmpdir) / "arx.json"
            arx.write_text(
                "{"
                '"schema": "arx2",'
                '"coefficients": [0.5, 0.25, -0.01, -0.005, 2.0, 1.0, 3.0]'
                "}\n",
                encoding="utf-8",
            )
            self.assertIsInstance(load_predictor(arx), Arx2ThermalModel)

    def test_zone_mpc_arx2_rollout_uses_previous_state(self):
        model = Arx2ThermalModel(
            temp_c=0.1,
            temp_prev_c=0.9,
            pwm=-0.01,
            pwm_prev=0.0,
            load=0.0,
            load_prev=0.0,
            bias=0.0,
        )
        controller = ZoneMpcController(model=model, horizon_steps=1)

        cooler_history = controller.decide(
            temp_c=60.0,
            load=0.0,
            current_pwm=0,
            prev_temp_c=50.0,
            prev_pwm=0,
            prev_load=0.0,
        )
        hotter_history = controller.decide(
            temp_c=60.0,
            load=0.0,
            current_pwm=0,
            prev_temp_c=70.0,
            prev_pwm=0,
            prev_load=0.0,
        )

        self.assertLess(cooler_history.terminal_temp_c, hotter_history.terminal_temp_c)

    def test_constant_plan_mode_keeps_single_pwm_plan(self):
        controller = ZoneMpcController(
            model=ThermalModel(a=1.0, b=-0.03, c=1.8, d=0.3),
            zone_low_temp_c=53.0,
            zone_high_temp_c=58.0,
            max_pwm_step=20,
            min_active_pwm=75,
            plan_mode="constant",
        )

        decision = controller.decide(temp_c=57.0, load=1.0, current_pwm=95)

        self.assertEqual(decision.plan_mode, "constant")
        self.assertEqual(decision.planned_pwms, [decision.pwm])

    def test_segmented_plan_mode_can_plan_future_pwm_moves(self):
        controller = ZoneMpcController(
            model=ThermalModel(a=0.96, b=-0.008, c=1.0, d=2.0),
            zone_low_temp_c=53.0,
            zone_high_temp_c=58.0,
            full_temp_c=69.0,
            safety_temp_c=70.0,
            max_pwm_step=20,
            min_active_pwm=75,
            horizon_steps=12,
            candidate_pwm_step=5,
            plan_mode="segmented",
            segments=3,
            segment_candidate_step=20,
        )
        constant_controller = ZoneMpcController(
            model=controller.model,
            zone_low_temp_c=53.0,
            zone_high_temp_c=58.0,
            full_temp_c=69.0,
            safety_temp_c=70.0,
            max_pwm_step=20,
            min_active_pwm=75,
            horizon_steps=12,
            candidate_pwm_step=5,
            plan_mode="constant",
        )

        segmented = controller.decide(temp_c=59.0, load=1.0, current_pwm=75)
        constant = constant_controller.decide(temp_c=59.0, load=1.0, current_pwm=75)

        self.assertEqual(segmented.plan_mode, "segmented")
        self.assertEqual(len(segmented.planned_pwms), 3)
        self.assertEqual(segmented.pwm, segmented.planned_pwms[0])
        self.assertGreater(max(segmented.planned_pwms), segmented.planned_pwms[0])
        self.assertGreaterEqual(segmented.pwm, constant.pwm)
        self.assertLessEqual(segmented.terminal_temp_c, constant.terminal_temp_c)
        self.assertLessEqual(segmented.cost, constant.cost)


if __name__ == "__main__":
    unittest.main()
