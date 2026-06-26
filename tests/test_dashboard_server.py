import tempfile
import unittest
from pathlib import Path

import dashboard_server


class DashboardServerTests(unittest.TestCase):
    def write_samples(self, directory: Path) -> Path:
        csv_path = directory / "shadow_samples.csv"
        csv_path.write_text(
            "\n".join(
                [
                    "timestamp,temp_c,pwm,rpm,load,freq_mhz",
                    "1000,52.0,0,0,0.10,1500",
                    "1002,58.5,75,2100,0.40,2400",
                    "1004,60.0,100,2800,0.60,2400",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return csv_path

    def test_summary_reports_configured_zone_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = dashboard_server.SampleStore(self.write_samples(Path(tmp)))

            summary = store.summary(10**12)

        self.assertEqual(summary["zone_low_c"], 53.0)
        self.assertEqual(summary["zone_high_c"], 58.0)
        self.assertAlmostEqual(summary["below_zone_low_pct"], 100.0 / 3.0)
        self.assertAlmostEqual(summary["over_zone_high_pct"], 200.0 / 3.0)
        self.assertAlmostEqual(summary["over_58_pct"], 200.0 / 3.0)

    def test_handler_supports_head_for_health_checks(self):
        self.assertTrue(hasattr(dashboard_server.DashboardHandler, "do_HEAD"))

    def test_dashboard_service_is_read_only_and_listens_on_8766(self):
        service = Path(__file__).resolve().parents[1] / "fan-control-dashboard.service"
        unit = service.read_text(encoding="utf-8")

        self.assertIn("dashboard_server.py", unit)
        self.assertIn("--port 8766", unit)
        self.assertIn("--host 0.0.0.0", unit)
        self.assertIn("SyslogIdentifier=fan-control-dashboard", unit)
        self.assertNotIn("fan_control.py", unit)

    def test_dashboard_html_displays_53_58_target_zone(self):
        html = (Path(__file__).resolve().parents[1] / "dashboard.html").read_text(encoding="utf-8")

        self.assertIn("53-58", html)
        self.assertIn("Samples above 58", html)
        self.assertIn("yTemp(58)", html)
        self.assertIn("yTemp(53)", html)


if __name__ == "__main__":
    unittest.main()
