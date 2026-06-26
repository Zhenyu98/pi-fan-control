# M2 ARX Predictor + Zone MPC Acceptance Report

Date: 2026-06-26

Note: this report records the earlier `50-60 C` target-zone acceptance run. The current service default has since been tightened to `53-58 C`.

## Scope

Implemented the M2 second-order ARX predictor as the active prediction model for Zone MPC fan control, then ran an 11 minute randomized stress acceptance test on the live Raspberry Pi 5 service.

The active service now uses:

- Model: `/home/pi/fan-control/data/model_arx2_m2.json`
- Control mode: `zone-mpc`
- Temperature zone: `50.0-60.0 C`
- `min_active_pwm=75`
- `max_step=20`
- `full_temp=69.0 C`
- `safety_temp=70.0 C`
- `safety_pwm=255`

## Implementation Summary

- Added `ThermalPredictor` protocol so the controller can support both legacy first-order models and ARX models.
- Added `Arx2ThermalModel` with state terms for previous temperature, PWM, and load.
- Added `load_predictor()` to load either legacy `{a,b,c,d}` model JSON or ARX2 model JSON.
- Updated Zone MPC rollout to call `predict_with_state()` and advance previous-state variables across the horizon.
- Updated the live loop to track previous measured temperature, previous PWM, and previous load.
- Kept shadow learning record-only for non-first-order models so an ARX deployment cannot be overwritten by the legacy first-order promoter.
- Updated random stress evaluation to score ARX2 one-step prediction using previous sample state.

## Model

The deployed M2 model came from the identification run:

- Source report: `data/identification/20260626-105237/identification_report.md`
- Deployed model: `/home/pi/fan-control/data/model_arx2_m2.json`

Coefficients:

```json
{
  "schema": "arx2",
  "feature_names": ["temp_c", "temp_prev_c", "pwm", "pwm_prev", "load", "load_prev", "bias"],
  "coefficients": [
    0.5567670422114871,
    0.4184214650812279,
    -0.0014623726815126798,
    0.000059901610051346036,
    0.681352157701699,
    -0.15616360945374425,
    1.2924654171170944
  ],
  "pwm_coefficient": -0.0014024710714613338,
  "load_coefficient": 0.5251885482479548
}
```

Sanity checks:

- PWM aggregate coefficient is negative.
- Load aggregate coefficient is positive.
- The model file is separate from `data/model.json`; `data/model.json` was not overwritten.

## Installed Service

Installed unit:

`/etc/systemd/system/fan-control.service`

ExecStart:

```text
/usr/bin/python3 /home/pi/fan-control/fan_control.py --model /home/pi/fan-control/data/model_arx2_m2.json --control-mode zone-mpc --zone-low 50 --zone-high 60 --full-temp 69 --idle-stop 50 --max-step 20 --min-active-pwm 75 --safety-temp 70 --safety-pwm 255 --shadow-learn --log-interval 30
```

Safety and reliability controls remain active:

- systemd `Restart=always`
- `ExecStopPost=/usr/bin/python3 /home/pi/fan-control/fan_safe.py`
- high temperature forced full speed via `safety_temp`
- ARX shadow learning is record-only, not active fan authority

## Random 11 Minute Stress Test

Run directory:

`/home/pi/fan-control/acceptance/random-stress-20260626-123759`

Command used a transient systemd unit:

```text
systemd-run --unit=fan-arx2-random --collect --working-directory=/home/pi/fan-control /usr/bin/python3 /home/pi/fan-control/random_stress_test.py --duration 660 --interval 2 --min-phase 20 --max-phase 75 --max-workers 4 --seed 26062611 --model /home/pi/fan-control/data/model_arx2_m2.json
```

Schedule:

- 15 randomized phases
- CPU workers ranged from 0 to 4
- Phase durations ranged from 19 to 64 seconds
- Total elapsed time: `661.13 s`

## Results

Temperature:

- Min: `46.85 C`
- Mean: `55.34 C`
- Max: `61.15 C`

Zone behavior:

- Samples over `60.0 C`: `38`
- Percent over zone high: `10.89%`
- Max overshoot above zone high: `1.15 C`
- Samples at or above full speed temperature `69.0 C`: `0`
- Samples at or above safety temperature `70.0 C`: `0`

PWM and RPM:

- PWM min/mean/max: `0.00 / 100.36 / 255.00`
- RPM min/mean/max: `0.00 / 3079.41 / 8915.00`

Prediction:

- Paired samples: `348`
- One-step MAE: `0.65 C`
- One-step RMSE: `1.00 C`
- One-step bias: `0.02 C`
- Direction accuracy: `0.669` over `281` significant samples

Controller overhead:

- CPU seconds over the run: `0.3000`
- CPU percent of one core: `0.045%`
- Max RSS: `14528 KiB`

Service health:

- Initial service state: `active/running`
- Final service state: `active/running`
- Main PID remained `107594`
- Restart delta: `0`
- Journal warnings: `0`

## Verification

Fresh verification commands:

```text
python3 -m unittest discover -s /home/pi/fan-control/tests
python3 -m py_compile /home/pi/fan-control/*.py
python3 /home/pi/fan-control/fan_control.py --dry-run --duration 6 --model /home/pi/fan-control/data/model_arx2_m2.json --control-mode zone-mpc --zone-low 50 --zone-high 60 --full-temp 69 --idle-stop 50 --max-step 20 --min-active-pwm 75 --safety-temp 70
systemctl show fan-control.service --property=ActiveState,SubState,MainPID,NRestarts,ExecMainStatus --no-pager
journalctl -u fan-control.service --since '2026-06-26 12:36:31' -p warning..alert --no-pager
```

Verification output:

- Unit tests: `34` tests passed.
- Python compile check: passed.
- Dry-run: passed and emitted Zone MPC ARX2 prediction fields.
- Service: `ActiveState=active`, `SubState=running`, `MainPID=107594`, `NRestarts=0`, `ExecMainStatus=0`.
- Warning-or-higher journal entries during the acceptance window: none.

Pytest note:

- `python3 -m pytest /home/pi/fan-control/tests -v` could not run because `pytest` is not installed in the current Python environment.
- The project test suite was verified through the standard-library `unittest` runner.

## Acceptance Conclusion

The M2 ARX predictor and Zone MPC controller passed the 11 minute randomized stress acceptance test.

Observed behavior was acceptable for that conservative 50-60 C acceptance zone:

- Temperature stayed below `62 C`.
- No full-speed threshold or safety threshold events occurred.
- The live service did not restart.
- The journal reported no warnings.
- Controller overhead was negligible compared with the cooling benefit.

The model is useful enough for active prediction, but not perfect. One-step RMSE was about `1.00 C`, and direction accuracy was about `67%`. Future model work should focus on better transient data, RPM-aware terms, and high-temperature rollout validation before enabling any ARX model auto-promotion.
