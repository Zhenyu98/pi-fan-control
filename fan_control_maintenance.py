#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Clean fan-control logs and experiment artifacts.")
    parser.add_argument("--acceptance-dir", default="/home/pi/fan-control/acceptance")
    parser.add_argument("--data-dir", default="/home/pi/fan-control/data")
    parser.add_argument("--artifact-retention-days", type=float, default=14.0)
    parser.add_argument("--keep-latest", type=int, default=5)
    parser.add_argument("--journal-vacuum-time", default="14d")
    parser.add_argument("--journal-vacuum-size", default="200M")
    parser.add_argument("--skip-journal", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def cleanup_artifacts(
    acceptance_dir: Path,
    data_dir: Path,
    retention_days: float,
    keep_latest: int,
    now_epoch: float | None = None,
    dry_run: bool = False,
) -> dict[str, list[str]]:
    now = time.time() if now_epoch is None else now_epoch
    cutoff = now - max(0.0, retention_days) * 86400.0
    candidate_groups = [_acceptance_candidates(acceptance_dir), _evaluation_candidates(data_dir)]
    candidates = [path for group in candidate_groups for path in group]
    protected: set[Path] = set()
    for group in candidate_groups:
        protected.update(_latest_paths(group, keep_latest))
    removed: list[str] = []
    would_remove: list[str] = []

    for path in candidates:
        if path in protected:
            continue
        if _mtime(path) > cutoff:
            continue
        destination = would_remove if dry_run else removed
        destination.append(str(path))
        if not dry_run:
            _remove_path(path)

    return {"removed": removed, "would_remove": would_remove}


def vacuum_journal(vacuum_time: str, vacuum_size: str, dry_run: bool = False) -> dict[str, object]:
    command = ["journalctl", f"--vacuum-time={vacuum_time}", f"--vacuum-size={vacuum_size}"]
    if dry_run:
        return {"command": command, "returncode": None, "stdout": "", "stderr": "", "dry_run": True}
    result = subprocess.run(command, check=False, text=True, capture_output=True)
    return {
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "dry_run": False,
    }


def _acceptance_candidates(acceptance_dir: Path) -> list[Path]:
    if not acceptance_dir.exists():
        return []
    candidates: list[Path] = []
    for path in acceptance_dir.iterdir():
        if path.name.startswith("."):
            continue
        if path.is_dir():
            candidates.append(path)
        elif path.name.startswith("arx2_") and path.suffix in {".md", ".json"}:
            candidates.append(path)
    return candidates


def _evaluation_candidates(data_dir: Path) -> list[Path]:
    if not data_dir.exists():
        return []
    return sorted(data_dir.glob("evaluation-*.json"))


def _latest_paths(paths: list[Path], keep_latest: int) -> set[Path]:
    if keep_latest <= 0:
        return set()
    return set(sorted(paths, key=_mtime, reverse=True)[:keep_latest])


def _mtime(path: Path) -> float:
    return path.stat().st_mtime


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def main() -> int:
    args = parse_args()
    artifact_result = cleanup_artifacts(
        acceptance_dir=Path(args.acceptance_dir),
        data_dir=Path(args.data_dir),
        retention_days=args.artifact_retention_days,
        keep_latest=args.keep_latest,
        dry_run=args.dry_run,
    )
    journal_result: dict[str, object] | None = None
    if not args.skip_journal:
        journal_result = vacuum_journal(args.journal_vacuum_time, args.journal_vacuum_size, dry_run=args.dry_run)

    print(json.dumps({"artifacts": artifact_result, "journal": journal_result}, indent=2))
    if journal_result is not None and journal_result.get("returncode") not in (None, 0):
        return int(journal_result["returncode"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
