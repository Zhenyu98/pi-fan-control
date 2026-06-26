#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import time
from pathlib import Path

from fan_control_io import CpuLoadMeter, SysfsFan


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect Raspberry Pi fan thermal samples.")
    parser.add_argument("--output", default="/home/pi/fan-control/data/samples.csv")
    parser.add_argument("--duration", type=float, default=600.0)
    parser.add_argument("--interval", type=float, default=2.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    fan = SysfsFan.discover()
    load_meter = CpuLoadMeter()
    fieldnames = ["timestamp", "temp_c", "pwm", "rpm", "load", "freq_mhz"]
    deadline = time.monotonic() + args.duration

    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        while time.monotonic() < deadline:
            writer.writerow(
                {
                    "timestamp": f"{time.time():.3f}",
                    "temp_c": f"{fan.read_temp_c():.3f}",
                    "pwm": fan.read_pwm(),
                    "rpm": fan.read_rpm(),
                    "load": f"{load_meter.read():.4f}",
                    "freq_mhz": f"{fan.read_freq_mhz():.1f}",
                }
            )
            handle.flush()
            time.sleep(args.interval)

    print(f"Wrote samples to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
