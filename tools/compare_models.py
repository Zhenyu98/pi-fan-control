#!/usr/bin/env python3
from __future__ import annotations

import _pathfix  # noqa: F401  # adds ../src to sys.path; must precede src imports

import argparse
import json
from pathlib import Path
from typing import Any

from model_identification import compare_models, read_samples_csv


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare Raspberry Pi fan thermal model candidates.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--rollout-steps", type=int, default=12)
    return parser.parse_args()


def fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def metric_value(result: dict[str, Any], group: str, key: str) -> Any:
    return result.get(group, {}).get(key)


def render_report(results: list[dict[str, Any]], rollout_key: str) -> str:
    lines = [
        "# Fan Model Comparison Report",
        "",
        "## Metrics",
        "",
        "| Model | Valid | 1-step MAE | 1-step RMSE | 1-step Bias | 12-step MAE | 12-step RMSE | 12-step Bias | High-temp Bias | PWM<0 | Load>0 | High-temp no low bias |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |",
    ]
    for result in results:
        constraints = result.get("constraints", {})
        valid = result.get("valid", False)
        lines.append(
            "| "
            + " | ".join(
                [
                    result.get("name", "unknown"),
                    str(valid),
                    fmt(metric_value(result, "one_step", "mae_c")),
                    fmt(metric_value(result, "one_step", "rmse_c")),
                    fmt(metric_value(result, "one_step", "bias_c")),
                    fmt(metric_value(result, rollout_key, "mae_c")),
                    fmt(metric_value(result, rollout_key, "rmse_c")),
                    fmt(metric_value(result, rollout_key, "bias_c")),
                    fmt(metric_value(result, "one_step_high_temp", "bias_c")),
                    str(constraints.get("pwm_coefficient_negative", False)),
                    str(constraints.get("load_coefficient_positive", False)),
                    str(constraints.get("high_temp_not_systematically_underestimated", False)),
                ]
            )
            + " |"
        )

    lines.extend(["", "## Invalid Models", ""])
    invalids = [result for result in results if not result.get("valid", False)]
    if not invalids:
        lines.append("None.")
    else:
        for result in invalids:
            lines.append(f"- `{result.get('name', 'unknown')}`: {result.get('error', 'unknown error')}")

    lines.extend(
        [
            "",
            "## Selection Notes",
            "",
            "- PWM coefficient must be negative.",
            "- Load coefficient must be positive.",
            "- High-temperature one-step and rollout bias should not be systematically negative.",
            "- Do not promote a model only because one-step error is low; the 12-step rollout matters for MPC.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    samples = read_samples_csv(args.input)
    results = compare_models(samples, rollout_steps=args.rollout_steps)
    input_path = Path(args.input)
    output_dir = Path(args.output_dir) if args.output_dir else input_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "model_comparison.json"
    report_path = output_dir / "model_comparison.md"
    json_path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    report_path.write_text(render_report(results, f"rollout_{args.rollout_steps}"), encoding="utf-8")
    print(f"Wrote {json_path}")
    print(f"Wrote {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
