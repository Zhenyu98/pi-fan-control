#!/usr/bin/env python3
"""Read-only Raspberry Pi fan-control live dashboard."""
from __future__ import annotations

import argparse
import csv
import json
import time
import subprocess
from dataclasses import dataclass
from http import HTTPStatus
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from statistics import mean
from urllib.parse import parse_qs, urlparse

DEFAULT_CSV = Path("/home/pi/fan-control/data/shadow_samples.csv")
DEFAULT_STATIC = Path("/home/pi/fan-control")
DEFAULT_ZONE_LOW_C = 53.0
DEFAULT_ZONE_HIGH_C = 58.0

@dataclass
class Sample:
    timestamp: float
    temp_c: float
    pwm: float
    rpm: float
    load: float
    freq_mhz: float

    def as_dict(self) -> dict[str, float]:
        return {
            "timestamp": self.timestamp,
            "temp_c": self.temp_c,
            "pwm": self.pwm,
            "rpm": self.rpm,
            "load": self.load,
            "freq_mhz": self.freq_mhz,
        }

class SampleStore:
    def __init__(
        self,
        csv_path: Path,
        zone_low_temp_c: float = DEFAULT_ZONE_LOW_C,
        zone_high_temp_c: float = DEFAULT_ZONE_HIGH_C,
    ):
        self.csv_path = csv_path
        self.zone_low_temp_c = zone_low_temp_c
        self.zone_high_temp_c = zone_high_temp_c

    def _candidate_files(self) -> list[Path]:
        rotated = self.csv_path.with_name(self.csv_path.name + ".1")
        return [p for p in (rotated, self.csv_path) if p.exists()]

    def read_samples(self, since_seconds: float | None = None, max_points: int = 1800) -> list[Sample]:
        rows: list[Sample] = []
        cutoff = time.time() - since_seconds if since_seconds else None
        for path in self._candidate_files():
            try:
                with path.open(newline="") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        try:
                            ts = float(row["timestamp"])
                            if cutoff is not None and ts < cutoff:
                                continue
                            rows.append(Sample(
                                timestamp=ts,
                                temp_c=float(row["temp_c"]),
                                pwm=float(row["pwm"]),
                                rpm=float(row["rpm"]),
                                load=float(row["load"]),
                                freq_mhz=float(row.get("freq_mhz") or 0.0),
                            ))
                        except (KeyError, ValueError):
                            continue
            except OSError:
                continue
        rows.sort(key=lambda s: s.timestamp)
        if max_points > 0 and len(rows) > max_points:
            last = rows[-1]
            step = max(1, len(rows) // max_points)
            rows = rows[::step]
            if rows and rows[-1].timestamp != last.timestamp:
                rows.append(last)
        return rows

    def latest(self) -> Sample | None:
        samples = self.read_samples(since_seconds=6 * 3600, max_points=0)
        return samples[-1] if samples else None

    def summary(self, since_seconds: float) -> dict[str, float | int | None]:
        samples = self.read_samples(since_seconds=since_seconds, max_points=0)
        if not samples:
            return {"samples": 0}
        temps = [s.temp_c for s in samples]
        pwms = [s.pwm for s in samples]
        rpms = [s.rpm for s in samples]
        loads = [s.load for s in samples]
        duration_s = samples[-1].timestamp - samples[0].timestamp if len(samples) > 1 else 0.0
        return {
            "samples": len(samples),
            "duration_s": duration_s,
            "zone_low_c": self.zone_low_temp_c,
            "zone_high_c": self.zone_high_temp_c,
            "temp_min_c": min(temps),
            "temp_mean_c": mean(temps),
            "temp_max_c": max(temps),
            "pwm_min": min(pwms),
            "pwm_mean": mean(pwms),
            "pwm_max": max(pwms),
            "rpm_min": min(rpms),
            "rpm_mean": mean(rpms),
            "rpm_max": max(rpms),
            "load_min": min(loads),
            "load_mean": mean(loads),
            "load_max": max(loads),
            "below_zone_low_pct": 100.0 * sum(t < self.zone_low_temp_c for t in temps) / len(temps),
            "over_zone_high_pct": 100.0 * sum(t > self.zone_high_temp_c for t in temps) / len(temps),
            "over_58_pct": 100.0 * sum(t > 58.0 for t in temps) / len(temps),
            "over_60_pct": 100.0 * sum(t > 60.0 for t in temps) / len(temps),
            "over_62_pct": 100.0 * sum(t > 62.0 for t in temps) / len(temps),
            "over_65_pct": 100.0 * sum(t > 65.0 for t in temps) / len(temps),
            "at_or_above_69": sum(t >= 69.0 for t in temps),
            "at_or_above_70": sum(t >= 70.0 for t in temps),
        }

def systemd_state(unit: str) -> dict[str, str | None]:
    try:
        out = subprocess.check_output(
            ["systemctl", "show", unit, "-p", "ActiveState", "-p", "SubState", "-p", "NRestarts", "--no-pager"],
            text=True,
            timeout=2,
        )
        data = {}
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                data[k] = v
        return {"active": data.get("ActiveState"), "sub": data.get("SubState"), "restarts": data.get("NRestarts")}
    except Exception:
        return {"active": None, "sub": None, "restarts": None}

class DashboardHandler(BaseHTTPRequestHandler):
    store: SampleStore
    static_dir: Path

    def log_message(self, fmt: str, *args) -> None:
        if self.path.startswith("/api/health"):
            return
        super().log_message(fmt, *args)

    def _send_json(self, payload: dict | list, status: int = 200, include_body: bool = True) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str, include_body: bool = True) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html", "/dashboard.html"):
            self._send_file(self.static_dir / "dashboard.html", "text/html; charset=utf-8", include_body=False)
            return
        if parsed.path.startswith("/api/"):
            self._send_json({"ok": True, "time": time.time()}, include_body=False)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path in ("/", "/index.html", "/dashboard.html"):
            self._send_file(self.static_dir / "dashboard.html", "text/html; charset=utf-8")
            return
        if parsed.path == "/api/health":
            self._send_json({"ok": True, "time": time.time()})
            return
        if parsed.path == "/api/latest":
            try:
                minutes = max(1.0, min(24 * 60.0, float(qs.get("minutes", ["60"])[0])))
                max_points = max(100, min(5000, int(qs.get("max_points", ["1800"])[0])))
            except ValueError:
                self._send_json({"error": "invalid query"}, status=400)
                return
            samples = self.store.read_samples(since_seconds=minutes * 60.0, max_points=max_points)
            self._send_json({
                "window_minutes": minutes,
                "zone": {"low_c": self.store.zone_low_temp_c, "high_c": self.store.zone_high_temp_c},
                "samples": [s.as_dict() for s in samples],
                "service": systemd_state("fan-control.service"),
            })
            return
        if parsed.path == "/api/status":
            latest = self.store.latest()
            self._send_json({
                "latest": latest.as_dict() if latest else None,
                "service": systemd_state("fan-control.service"),
                "zone": {"low_c": self.store.zone_low_temp_c, "high_c": self.store.zone_high_temp_c},
                "csv_path": str(self.store.csv_path),
                "server_time": time.time(),
            })
            return
        if parsed.path == "/api/summary":
            try:
                hours = max(0.1, min(24.0 * 7.0, float(qs.get("hours", ["4"])[0])))
            except ValueError:
                self._send_json({"error": "invalid query"}, status=400)
                return
            self._send_json({"window_hours": hours, "summary": self.store.summary(hours * 3600.0)})
            return
        self.send_error(HTTPStatus.NOT_FOUND)

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--static", type=Path, default=DEFAULT_STATIC)
    parser.add_argument("--zone-low", type=float, default=DEFAULT_ZONE_LOW_C)
    parser.add_argument("--zone-high", type=float, default=DEFAULT_ZONE_HIGH_C)
    args = parser.parse_args()
    DashboardHandler.store = SampleStore(args.csv, zone_low_temp_c=args.zone_low, zone_high_temp_c=args.zone_high)
    DashboardHandler.static_dir = args.static
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"fan-control dashboard listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()

if __name__ == "__main__":
    main()
