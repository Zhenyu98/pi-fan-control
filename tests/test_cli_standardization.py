import sys
import unittest
from io import StringIO
from unittest.mock import patch

import fan_control


class CliStandardizationTests(unittest.TestCase):
    def test_default_runtime_is_arx2_zone_mpc(self):
        with patch.object(sys, "argv", ["fan_control.py"]):
            args = fan_control.parse_args()

        self.assertEqual(args.control_mode, "zone-mpc")
        self.assertEqual(args.model, "/home/pi/fan-control/data/model_arx2_m2.json")
        self.assertEqual(args.zone_low, 50.0)
        self.assertEqual(args.zone_high, 60.0)
        self.assertEqual(args.max_step, 20)
        self.assertEqual(args.idle_stop, 50.0)
        self.assertEqual(args.log_interval, 30.0)

    def test_unsupported_control_mode_is_rejected(self):
        with patch.object(sys, "argv", ["fan_control.py", "--control-mode", "legacy"]):
            with patch("sys.stderr", StringIO()), self.assertRaises(SystemExit):
                fan_control.parse_args()

    def test_decision_logging_is_rate_limited_but_keeps_pwm_events(self):
        self.assertTrue(
            fan_control.should_log_decision(
                now_monotonic=100.0,
                last_log_monotonic=None,
                log_interval_s=30.0,
                current_pwm=0,
                next_pwm=0,
                reason="inside_zone",
            )
        )
        self.assertFalse(
            fan_control.should_log_decision(
                now_monotonic=110.0,
                last_log_monotonic=100.0,
                log_interval_s=30.0,
                current_pwm=0,
                next_pwm=0,
                reason="inside_zone",
            )
        )
        self.assertTrue(
            fan_control.should_log_decision(
                now_monotonic=110.0,
                last_log_monotonic=100.0,
                log_interval_s=30.0,
                current_pwm=0,
                next_pwm=75,
                reason="inside_zone",
            )
        )
        self.assertTrue(
            fan_control.should_log_decision(
                now_monotonic=110.0,
                last_log_monotonic=100.0,
                log_interval_s=30.0,
                current_pwm=75,
                next_pwm=75,
                reason="sustained_violation",
            )
        )


if __name__ == "__main__":
    unittest.main()
