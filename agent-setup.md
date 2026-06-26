# Agent Setup

## Copy-Paste Prompt

```text
Please read the repository README and help me install the Raspberry Pi 5 fan-control service.
Goal: run the Zone MPC fan controller safely as a systemd service.
Before changing files, writing system paths, enabling services, or touching PWM/sysfs, show me the plan and ask for approval.
Run non-destructive checks first, then report the exact files changed, commands run, service status, and verification result.
```

## Prerequisites

- Raspberry Pi 5 with the official fan connected to the fan header.
- Raspberry Pi OS with Python 3 and systemd.
- `sudo` access for installing systemd units and writing PWM/sysfs controls.
- A safe fallback fan policy in `/boot/firmware/config.txt` or confidence that `fan_safe.py` can restore a safe state on service stop.

## Setup Steps

1. Inspect the README, service files, and current `/sys/class/hwmon` fan path.
2. Run a dry check:

```bash
python3 /home/pi/fan-control/fan_control.py --dry-run --duration 20
```

3. Install the service files only after user approval:

```bash
sudo cp /home/pi/fan-control/fan-control.service /etc/systemd/system/fan-control.service
sudo cp /home/pi/fan-control/fan-control-maintenance.service /etc/systemd/system/fan-control-maintenance.service
sudo cp /home/pi/fan-control/fan-control-maintenance.timer /etc/systemd/system/fan-control-maintenance.timer
sudo systemctl daemon-reload
sudo systemctl enable --now fan-control.service
sudo systemctl enable --now fan-control-maintenance.timer
```

4. Smoke test:

```bash
systemctl status fan-control.service
journalctl -u fan-control.service -n 50 --no-pager
cat /sys/class/hwmon/hwmon*/name
```

## Success Signal

- `fan-control.service` is `active/running`.
- The log contains `mode=zone-mpc` and prediction fields.
- No warning-or-higher journal entries appear during the smoke test.
- Fan RPM follows nonzero PWM when temperature or load rises.

## Safety Rules

- Do not read or print secrets.
- Do not publish, push, delete, or deploy without explicit approval.
- Do not leave the fan stopped after tests.
- Do not run long stress tests before a short dry run and service smoke test pass.
- If temperature reaches the safety threshold, prefer full speed and stop the experiment.
