# Raspberry Pi 5 Advanced Fan Controller

A Raspberry Pi 5 advanced fan controller using Zone MPC.

Instead of waiting for the SoC to cross a few fixed temperature thresholds, this project predicts the near-future thermal trajectory and chooses a PWM that keeps the machine inside a target temperature zone.

The user-visible result is a fan strategy that is more active, cooler, and more controllable:

- The temperature curve is smoother and less likely to overshoot.
- The fan may start earlier, but it does not have to be louder overall.
- The controller uses negligible CPU and memory on the Pi 5.
- Within the temperature target, Zone MPC prefers the lowest PWM cost that still protects the thermal zone.

Implementation standard:

```text
M2 second-order ARX predictor + Zone MPC
```

## Why This Exists

The stock kernel fan policy is simple and robust, but it is reactive: it maps temperature thresholds to fixed PWM steps. That is easy to reason about, but it cannot ask a richer question:

```text
Given current temperature, fan PWM, CPU load, and recent history,
what PWM keeps the next few control steps inside the desired zone
with the least fan effort?
```

This controller answers that question every control interval.

## Current Product Decision

This project is standardized on **Zone MPC** with a second-order ARX thermal predictor.

The previous single-setpoint controller has been removed from the runtime path. Historical reports may mention earlier experiments, but the supported controller is now the predictive zone controller.

Default policy:

- Target zone: `50-60 C`
- Stable first fan step: `min_active_pwm=75`
- PWM ramp limit: `max_step=20`
- Full speed threshold: `69 C`
- Safety threshold: `70 C`
- Model: `/home/pi/fan-control/data/model_arx2_m2.json`

## How It Works

### M2 ARX Predictor

The active model is a second-order ARX thermal predictor:

```text
T_next =
  a0 * T_now
+ a1 * T_previous
+ b0 * PWM_now
+ b1 * PWM_previous
+ c0 * load_now
+ c1 * load_previous
+ bias
```

Using previous temperature, previous PWM, and previous load makes the model better at short rollout prediction than a simple first-order model. This matters because MPC decisions are based on a horizon, not just one immediate step.

### Zone MPC Controller

Zone MPC evaluates candidate PWM values and rolls the model forward over the prediction horizon.

It prefers:

- temperatures inside the `50-60 C` zone,
- low PWM,
- small PWM changes,
- no sustained predicted violation above `60 C`,
- no predicted approach to the `69 C` full-speed threshold.

It also includes a conservative prediction observer. If the model has recently underpredicted temperature, the controller adds a bounded prediction margin, so future MPC scoring becomes more cautious.

## Measured Result

Latest randomized 11 minute stress test:

```text
Run: /home/pi/fan-control/acceptance/random-stress-20260626-123759
Model: /home/pi/fan-control/data/model_arx2_m2.json
```

Summary:

- Temperature min/mean/max: `46.85 / 55.34 / 61.15 C`
- Samples above `60 C`: `10.89%`
- Max overshoot above `60 C`: `1.15 C`
- Samples at or above `69 C`: `0`
- Samples at or above `70 C`: `0`
- One-step prediction MAE/RMSE/Bias: `0.65 / 1.00 / 0.02 C`
- Controller CPU use: `0.045%` of one core
- Max RSS: `14528 KiB`
- Service restart delta: `0`
- Journal warnings: `0`

Detailed report:

```text
/home/pi/fan-control/acceptance/arx2_zone_mpc_acceptance_20260626.md
```

## Files

- `fan_control.py`: live ARX2 Zone MPC controller.
- `fan_control_core.py`: thermal models, ARX2 fitting, prediction observer, Zone MPC.
- `fan_control_shadow.py`: shadow learning and safe model promotion.
- `fan_control_io.py`: sysfs discovery and fan I/O.
- `fan_safe.py`: systemd stop-post fallback.
- `collect.py`: data collection.
- `fit_model.py`: ARX2 model fitting from collected samples.
- `identify_model.py`: dedicated load/PWM identification experiment.
- `compare_models.py`: model comparison report generator.
- `evaluate.py`: pressure test and controller overhead measurement.
- `random_stress_test.py`: randomized pressure phases with prediction metrics.
- `fan-control.service`: systemd service template.
- `fan-control-maintenance.service`: cleanup service for journal and experiment artifacts.
- `fan-control-maintenance.timer`: daily cleanup timer.

The scripts auto-discover the current `/sys/class/hwmon/hwmon*/name == pwmfan` device at startup, so they do not depend on the `hwmonN` number staying fixed across reboots.

## Quick Check

Run without touching PWM:

```bash
python3 /home/pi/fan-control/fan_control.py --dry-run --duration 20
```

Expected log fields include:

```text
mode=zone-mpc
prediction_margin=...
predicted_max=...
terminal=...
violation_steps=...
reason=...
```

## Run Manually

Run the controller for two minutes:

```bash
sudo python3 /home/pi/fan-control/fan_control.py --duration 120
```

The controller restores automatic fan mode on exit.

On this Pi, `pwm1_enable=1` is the effective writable mode. A quick probe showed PWM `40` does not spin the fan, PWM `60` barely spins it, and PWM `75` is the stable first step. The controller therefore avoids tiny non-spinning PWM outputs.

## Shadow Learning

The service runs with `--shadow-learn`.

Shadow learning does not directly control the fan. The active controller keeps using the current stable ARX2 model while the learner records live samples to:

```text
/home/pi/fan-control/data/shadow_samples.csv
```

Every check interval, it fits a candidate model from the rolling sample window. For the current runtime, the candidate is also ARX2. Promotion requires:

- enough samples,
- enough temperature, PWM, and load variation,
- aggregate PWM coefficient remains cooling,
- aggregate load coefficient remains heating,
- ARX temperature memory stays inside conservative bounds,
- high-temperature bias is not systemically underpredicting,
- prediction MAE improves by the configured minimum.

If accepted, the learner atomically writes the current model path and hot-loads the new controller. It keeps only:

```text
current model
previous model
```

The sample log rotates to one `.1` file, so long-running learning data does not grow without bound.

## Logging And Retention

The live controller no longer writes routine journal logs every control interval. By default, `fan_control.py` logs one routine status line every `30` seconds:

```bash
python3 /home/pi/fan-control/fan_control.py --log-interval 30
```

Important events still log immediately:

- PWM changes,
- sustained predicted zone violations,
- full-speed or safety decisions,
- shadow-learning decisions and promotions.

The systemd unit also sets journald rate-limit fields as a backstop:

```text
LogRateLimitIntervalSec=60
LogRateLimitBurst=120
```

Cleanup is handled by `fan_control_maintenance.py`. The default policy keeps recent artifacts and removes older experiment output:

```bash
python3 /home/pi/fan-control/fan_control_maintenance.py --dry-run
sudo python3 /home/pi/fan-control/fan_control_maintenance.py
```

Default retention:

- acceptance artifacts: remove old run directories after `14` days, while keeping the latest `5`,
- evaluation artifacts: remove old `data/evaluation-*.json` files after `14` days, while keeping the latest `5`,
- journal: run `journalctl --vacuum-time=14d --vacuum-size=200M`.

Note: journal vacuum is global to systemd-journald, not per service. The controller reduces its own log volume with `--log-interval`; the vacuum command keeps the journal store bounded.

## Collect And Fit

For a quick local fit from collected samples:

```bash
python3 /home/pi/fan-control/collect.py --duration 600 --interval 2
python3 /home/pi/fan-control/fit_model.py
```

For a better model, use the dedicated identification experiment:

```bash
sudo python3 /home/pi/fan-control/identify_model.py
python3 /home/pi/fan-control/compare_models.py --input /home/pi/fan-control/data/identification/<run>/samples.csv
```

The model selection rule is intentionally conservative:

- do not promote a model only because one-step error is low,
- check rollout error because MPC depends on prediction horizon,
- require cooling sign for PWM,
- require heating sign for load,
- reject high-temperature systematic underprediction.

## Evaluate

Run a short pressure test and overhead measurement:

```bash
sudo python3 /home/pi/fan-control/evaluate.py --duration 90 --workers 4
```

Run randomized pressure phases:

```bash
python3 /home/pi/fan-control/random_stress_test.py --duration 660 --interval 2 --min-phase 20 --max-phase 75 --max-workers 4 --model /home/pi/fan-control/data/model_arx2_m2.json
```

For a no-write check:

```bash
python3 /home/pi/fan-control/evaluate.py --duration 30 --workers 2 --dry-run
```

## Service

Install:

```bash
sudo cp /home/pi/fan-control/fan-control.service /etc/systemd/system/fan-control.service
sudo cp /home/pi/fan-control/fan-control-maintenance.service /etc/systemd/system/fan-control-maintenance.service
sudo cp /home/pi/fan-control/fan-control-maintenance.timer /etc/systemd/system/fan-control-maintenance.timer
sudo systemctl daemon-reload
sudo systemctl enable --now fan-control.service
sudo systemctl enable --now fan-control-maintenance.timer
```

Check:

```bash
systemctl status fan-control.service
journalctl -u fan-control.service -n 50 --no-pager
systemctl list-timers fan-control-maintenance.timer
```

Disable and return to the kernel fan policy:

```bash
sudo systemctl disable --now fan-control.service
sudo systemctl disable --now fan-control-maintenance.timer
printf '1\n' | sudo tee /sys/devices/platform/cooling_fan/hwmon/hwmon2/pwm1_enable
```

## Kernel Fan Policy

The current `/boot/firmware/config.txt` does not contain active custom `dtparam=fan_temp*` or `dtparam=cooling_fan` lines.

While `fan-control.service` is running, the user-space controller owns PWM. If the service stops, `ExecStopPost` runs `fan_safe.py`, which sets a safe PWM based on current temperature.

If custom `fan_temp*` entries are added later, treat them as a fallback policy rather than the primary controller.
