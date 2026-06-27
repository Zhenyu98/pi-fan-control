from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_TEMP_PATH = Path("/sys/class/thermal/thermal_zone0/temp")
DEFAULT_PWM_PATH = Path("/sys/devices/platform/cooling_fan/hwmon/hwmon2/pwm1")
DEFAULT_ENABLE_PATH = Path("/sys/devices/platform/cooling_fan/hwmon/hwmon2/pwm1_enable")
DEFAULT_RPM_PATH = Path("/sys/devices/platform/cooling_fan/hwmon/hwmon2/fan1_input")
DEFAULT_FREQ_PATH = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")


@dataclass(frozen=True)
class PwmFanPaths:
    pwm_path: Path
    enable_path: Path
    rpm_path: Path


def discover_pwmfan(hwmon_root: str | Path = "/sys/class/hwmon") -> PwmFanPaths:
    root = Path(hwmon_root)
    for candidate in sorted(root.glob("hwmon*")):
        name_path = candidate / "name"
        try:
            name = name_path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if name != "pwmfan":
            continue

        paths = PwmFanPaths(
            pwm_path=candidate / "pwm1",
            enable_path=candidate / "pwm1_enable",
            rpm_path=candidate / "fan1_input",
        )
        if all(path.exists() for path in (paths.pwm_path, paths.enable_path, paths.rpm_path)):
            return paths

    if all(path.exists() for path in (DEFAULT_PWM_PATH, DEFAULT_ENABLE_PATH, DEFAULT_RPM_PATH)):
        return PwmFanPaths(
            pwm_path=DEFAULT_PWM_PATH,
            enable_path=DEFAULT_ENABLE_PATH,
            rpm_path=DEFAULT_RPM_PATH,
        )

    raise FileNotFoundError("could not find hwmon device named pwmfan")


def read_float(path: str | Path, scale: float = 1.0) -> float:
    return float(Path(path).read_text(encoding="utf-8").strip()) / scale


def read_int(path: str | Path) -> int:
    return int(float(Path(path).read_text(encoding="utf-8").strip()))


def write_int(path: str | Path, value: int | float, minimum: int, maximum: int) -> None:
    clamped = max(minimum, min(maximum, int(round(value))))
    Path(path).write_text(f"{clamped}\n", encoding="utf-8")


@dataclass(frozen=True)
class SysfsFan:
    temp_path: Path = DEFAULT_TEMP_PATH
    pwm_path: Path = DEFAULT_PWM_PATH
    enable_path: Path = DEFAULT_ENABLE_PATH
    rpm_path: Path = DEFAULT_RPM_PATH
    freq_path: Path = DEFAULT_FREQ_PATH

    @classmethod
    def discover(cls) -> "SysfsFan":
        paths = discover_pwmfan()
        return cls(
            temp_path=DEFAULT_TEMP_PATH,
            pwm_path=paths.pwm_path,
            enable_path=paths.enable_path,
            rpm_path=paths.rpm_path,
            freq_path=DEFAULT_FREQ_PATH,
        )

    def read_temp_c(self) -> float:
        return read_float(self.temp_path, scale=1000.0)

    def read_pwm(self) -> int:
        return read_int(self.pwm_path)

    def read_rpm(self) -> int:
        return read_int(self.rpm_path)

    def read_freq_mhz(self) -> float:
        if not self.freq_path.exists():
            return 0.0
        return read_float(self.freq_path, scale=1000.0)

    def set_manual(self) -> None:
        write_int(self.enable_path, 1, minimum=0, maximum=1)

    def restore_auto(self) -> None:
        write_int(self.enable_path, 1, minimum=0, maximum=1)

    def write_pwm(self, pwm: int) -> None:
        write_int(self.pwm_path, pwm, minimum=0, maximum=255)


class CpuLoadMeter:
    def __init__(self, stat_path: str | Path = "/proc/stat") -> None:
        self.stat_path = Path(stat_path)
        self.previous = self._read_cpu_times()

    def _read_cpu_times(self) -> tuple[int, int]:
        fields = self.stat_path.read_text(encoding="utf-8").splitlines()[0].split()
        values = [int(value) for value in fields[1:]]
        idle = values[3] + values[4]
        total = sum(values)
        return total, idle

    def read(self) -> float:
        current_total, current_idle = self._read_cpu_times()
        previous_total, previous_idle = self.previous
        self.previous = current_total, current_idle
        total_delta = current_total - previous_total
        idle_delta = current_idle - previous_idle
        if total_delta <= 0:
            return 0.0
        return max(0.0, min(1.0, 1.0 - idle_delta / total_delta))


def ensure_root_for_writes() -> None:
    if os.geteuid() != 0:
        raise PermissionError("fan PWM writes require root; run with sudo")
