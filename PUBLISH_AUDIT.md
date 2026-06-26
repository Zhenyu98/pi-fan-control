# Publish Audit

Date: 2026-06-26

## Scope

Prepared the Raspberry Pi fan controller for publication as `pi-fan-control`.

The publishable product is:

```text
Raspberry Pi 5 advanced fan controller using Zone MPC
```

Current default target zone is `53-58 C`.

## Local Installation Status

Installed and enabled on this Raspberry Pi:

- `fan-control.service`
- `fan-control-maintenance.service`
- `fan-control-maintenance.timer`

Observed service state after install:

- `fan-control.service`: `active/running`
- `MainPID`: `127106`
- `NRestarts`: `0`
- previous service: `inactive/dead`, disabled
- maintenance timer: scheduled for the next daily run

The live service uses:

```text
/usr/bin/python3 /home/pi/fan-control/fan_control.py
```

`/home/pi/fan-control` is a local compatibility symlink to the current workspace.

## Publish Readiness

Ready to publish:

- Source code renamed to `fan_control*`.
- Runtime entrypoint is `fan_control.py`.
- systemd unit is `fan-control.service`.
- Read-only dashboard is included as `dashboard_server.py`, `dashboard.html`, and `fan-control-dashboard.service`.
- Maintenance unit and timer are included.
- README explains the product story and operating model.
- `README_zh.md` provides the Simplified Chinese project guide.
- `agent-setup.md` is bilingual and optimized for agent-assisted installation.
- `LICENSE` is included.
- `.gitignore` excludes runtime data, raw captures, caches, logs, and generated files.

Current remote status:

- `/home/pi/fan-control` is initialized as a local git repository on branch `main`.
- GitHub CLI `gh` is installed and authenticated as `Zhenyu98`.
- Remote `origin` points to `https://github.com/Zhenyu98/pi-fan-control.git`.
- `Zhenyu98/pi-fan-control` is public.
- A documentation remediation update is pending confirmation before push.

## Sensitive Data Check

Searched source/docs for common token, secret, password, API key, GitHub PAT, OpenAI key, and private key patterns, excluding cache/raw data/log files and this audit document.

Result:

- No real secrets, tokens, private keys, personal emails, or credential values were found in source/docs after excluding binary/cache/raw CSV/log artifacts.
- Expected false positives remain in `.gitignore`, safety documentation, and code that references `SUDO_UID`, `SUDO_GID`, and `ACCEPTANCE_RUN_DIR`.

## Artifact Policy

The following are intentionally not for GitHub:

- Python caches: `__pycache__/`, `*.pyc`
- runtime shadow data: `data/shadow_samples.csv`
- local evaluation output: `data/evaluation-*.json`
- raw acceptance run directories: `acceptance/*/`
- generated real-workload dashboard reports: `acceptance/real_workload_*`, `docs/assets/zone_mpc_real_workload_*.svg`
- raw identification samples and metadata: `data/identification/*/samples.csv`, `summary.json`
- ad-hoc logs and pid files

Curated publishable artifacts:

- `README.md`
- `README_zh.md`
- `PUBLISH_AUDIT.md`
- `agent-setup.md`
- `LICENSE`
- `dashboard.html`
- `dashboard_server.py`
- `fan-control-dashboard.service`
- `acceptance/arx2_zone_mpc_acceptance_20260626.md`
- `data/model_arx2_m2.json`

## Validation

Fresh checks:

```text
python3 -m unittest discover -s /home/pi/fan-control/tests
python3 -m py_compile /home/pi/fan-control/*.py
systemd-analyze verify /home/pi/fan-control/fan-control.service /home/pi/fan-control/fan-control-dashboard.service /home/pi/fan-control/fan-control-maintenance.service /home/pi/fan-control/fan-control-maintenance.timer
python3 /home/pi/fan-control/fan_control.py --dry-run --duration 4
curl http://127.0.0.1:8766/api/status
```

Results:

- Unit tests: passed.
- Python compile check: passed.
- systemd unit verification: passed.
- dry-run: passed.
- dashboard unit and HTTP smoke test: passed with `zone.low_c=53.0` and `zone.high_c=58.0`.

Pytest note:

- `python3 -m pytest /home/pi/fan-control/tests -v` cannot run because `pytest` is not installed in the current Python environment.

## Next GitHub Step

After the user confirms the update packet, push the control-target and dashboard integration commit:

```bash
cd /home/pi/fan-control
git push origin main
```
