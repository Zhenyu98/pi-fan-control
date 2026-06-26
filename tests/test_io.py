import tempfile
import unittest
from pathlib import Path

from fan_control_io import SysfsFan, discover_pwmfan, read_float, write_int


class IoTests(unittest.TestCase):
    def test_read_float_converts_millicelsius(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "temp"
            path.write_text("53250\n", encoding="utf-8")

            self.assertEqual(read_float(path, scale=1000.0), 53.25)

    def test_write_int_clamps_and_writes_decimal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pwm1"

            write_int(path, 999, minimum=0, maximum=255)

            self.assertEqual(path.read_text(encoding="utf-8"), "255\n")

    def test_sysfs_fan_uses_configured_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            temp = root / "temp"
            pwm = root / "pwm1"
            enable = root / "pwm1_enable"
            rpm = root / "fan1_input"
            temp.write_text("47000\n", encoding="utf-8")
            pwm.write_text("75\n", encoding="utf-8")
            enable.write_text("1\n", encoding="utf-8")
            rpm.write_text("1800\n", encoding="utf-8")

            fan = SysfsFan(temp_path=temp, pwm_path=pwm, enable_path=enable, rpm_path=rpm)

            self.assertEqual(fan.read_temp_c(), 47.0)
            self.assertEqual(fan.read_pwm(), 75)
            self.assertEqual(fan.read_rpm(), 1800)
            fan.set_manual()
            self.assertEqual(enable.read_text(encoding="utf-8"), "1\n")
            fan.write_pwm(42)
            fan.restore_auto()

            self.assertEqual(enable.read_text(encoding="utf-8"), "1\n")
            self.assertEqual(pwm.read_text(encoding="utf-8"), "42\n")

    def test_discover_pwmfan_finds_matching_hwmon_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wrong = root / "hwmon0"
            right = root / "hwmon7"
            wrong.mkdir()
            right.mkdir()
            (wrong / "name").write_text("cpu_thermal\n", encoding="utf-8")
            (right / "name").write_text("pwmfan\n", encoding="utf-8")
            for filename in ("pwm1", "pwm1_enable", "fan1_input"):
                (right / filename).write_text("0\n", encoding="utf-8")

            paths = discover_pwmfan(hwmon_root=root)

            self.assertEqual(paths.pwm_path, right / "pwm1")
            self.assertEqual(paths.enable_path, right / "pwm1_enable")
            self.assertEqual(paths.rpm_path, right / "fan1_input")


if __name__ == "__main__":
    unittest.main()
