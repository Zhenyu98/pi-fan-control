#!/usr/bin/env python3
from __future__ import annotations

import _pathfix  # noqa: F401  # adds ../src to sys.path; must precede src imports

import argparse
from pathlib import Path

from fan_control_core import fit_arx2_thermal_model, read_csv_samples


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fit an ARX2 Raspberry Pi fan thermal model for Zone MPC.")
    parser.add_argument("--input", default="/home/pi/fan-control/data/samples.csv")
    parser.add_argument("--output", default="/home/pi/fan-control/data/model_arx2_m2.json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    samples = read_csv_samples(args.input)
    model = fit_arx2_thermal_model(samples)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    model.save(output)
    print(
        "Fitted ARX2 model: "
        f"temp={model.temp_c:.6f} temp_prev={model.temp_prev_c:.6f} "
        f"pwm={model.pwm:.6f} pwm_prev={model.pwm_prev:.6f} "
        f"load={model.load:.6f} load_prev={model.load_prev:.6f} "
        f"bias={model.bias:.6f}"
    )
    print(f"Wrote model to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
