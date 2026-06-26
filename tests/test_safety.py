import tempfile
import unittest
from pathlib import Path

from fan_control_io import SysfsFan
from fan_safe import safe_pwm_for_temp, set_safe_fan_state


class SafetyTests(unittest.TestCase):
    def test_safe_pwm_for_temp_uses_full_speed_at_high_temperature(self):
        self.assertEqual(safe_pwm_for_temp(73.0), 255)
        self.assertEqual(safe_pwm_for_temp(55.0), 75)
        self.assertEqual(safe_pwm_for_temp(45.0), 0)

    def test_set_safe_fan_state_writes_enable_and_pwm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            temp = root / "temp"
            pwm = root / "pwm1"
            enable = root / "pwm1_enable"
            rpm = root / "fan1_input"
            temp.write_text("73000\n", encoding="utf-8")
            pwm.write_text("0\n", encoding="utf-8")
            enable.write_text("1\n", encoding="utf-8")
            rpm.write_text("0\n", encoding="utf-8")
            fan = SysfsFan(temp_path=temp, pwm_path=pwm, enable_path=enable, rpm_path=rpm)

            set_safe_fan_state(fan)

            self.assertEqual(enable.read_text(encoding="utf-8"), "1\n")
            self.assertEqual(pwm.read_text(encoding="utf-8"), "255\n")


if __name__ == "__main__":
    unittest.main()
