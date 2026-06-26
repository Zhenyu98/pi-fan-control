#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys

from fan_control_io import SysfsFan, ensure_root_for_writes


def safe_pwm_for_temp(temp_c: float, warm_temp_c: float = 50.0, high_temp_c: float = 70.0) -> int:
    if temp_c >= high_temp_c:
        return 255
    if temp_c >= warm_temp_c:
        return 75
    return 0


def set_safe_fan_state(fan: SysfsFan, warm_temp_c: float = 50.0, high_temp_c: float = 70.0) -> int:
    temp_c = fan.read_temp_c()
    pwm = safe_pwm_for_temp(temp_c, warm_temp_c=warm_temp_c, high_temp_c=high_temp_c)
    fan.set_manual()
    fan.write_pwm(pwm)
    return pwm


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Set Raspberry Pi fan to a safe fallback state.")
    parser.add_argument("--warm-temp", type=float, default=50.0)
    parser.add_argument("--high-temp", type=float, default=70.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_root_for_writes()
    fan = SysfsFan.discover()
    pwm = set_safe_fan_state(fan, warm_temp_c=args.warm_temp, high_temp_c=args.high_temp)
    print(f"fan-safe: temp={fan.read_temp_c():.2f}C pwm={pwm}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"fan-safe: failed: {exc}", file=sys.stderr, flush=True)
        raise
