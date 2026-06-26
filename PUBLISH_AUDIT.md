# Publish Audit

Date: 2026-06-26

## Scope

Prepared the Raspberry Pi fan controller for publication as `pi-fan-control`.

The publishable product is:

```text
Raspberry Pi 5 advanced fan controller using Zone MPC
```

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
- Maintenance unit and timer are included.
- README explains the product story and operating model.
- `.gitignore` excludes runtime data, raw captures, caches, logs, and generated files.

Current status before pushing from this machine:

- `/home/pi/fan-control` is initialized as a local git repository on branch `main`.
- GitHub CLI `gh` is installed and authenticated as `Zhenyu98`.
- No GitHub remote is configured locally.
- The target repository full name still needs explicit confirmation.
- `Zhenyu98/pi-fan-control` is the current target repository name pending final confirmation.

## Sensitive Data Check

Searched source/docs for common token, secret, password, API key, GitHub PAT, OpenAI key, and private key patterns, excluding cache/raw data/log files and this audit document.

Result:

- No matches in source/docs after excluding binary/cache/raw CSV/log artifacts.

## Artifact Policy

The following are intentionally not for GitHub:

- Python caches: `__pycache__/`, `*.pyc`
- runtime shadow data: `data/shadow_samples.csv`
- local evaluation output: `data/evaluation-*.json`
- raw acceptance run directories: `acceptance/*/`
- raw identification samples and metadata: `data/identification/*/samples.csv`, `summary.json`
- ad-hoc logs and pid files

Curated publishable artifacts:

- `README.md`
- `PUBLISH_AUDIT.md`
- `acceptance/arx2_zone_mpc_acceptance_20260626.md`
- `data/model_arx2_m2.json`

## Validation

Fresh checks:

```text
python3 -m unittest discover -s /home/pi/fan-control/tests
python3 -m py_compile /home/pi/fan-control/*.py
systemd-analyze verify /home/pi/fan-control/fan-control.service /home/pi/fan-control/fan-control-maintenance.service /home/pi/fan-control/fan-control-maintenance.timer
python3 /home/pi/fan-control/fan_control.py --dry-run --duration 4
```

Results:

- Unit tests: passed.
- Python compile check: passed.
- systemd unit verification: passed.
- dry-run: passed.

Pytest note:

- `python3 -m pytest /home/pi/fan-control/tests -v` cannot run because `pytest` is not installed in the current Python environment.

## Next GitHub Step

To publish through GitHub, provide the target repository name in `owner/repo` form, or initialize this directory as a git checkout connected to that remote.

Recommended local publish sequence after repository selection:

```bash
cd /home/pi/fan-control
git init
git branch -M main
git remote add origin git@github.com:OWNER/REPO.git
git add .gitignore README.md PUBLISH_AUDIT.md agent-setup.md *.py *.service *.timer tests acceptance/arx2_zone_mpc_acceptance_20260626.md data/model_arx2_m2.json
git commit -m "Prepare fan-control for publication"
git push -u origin main
```
