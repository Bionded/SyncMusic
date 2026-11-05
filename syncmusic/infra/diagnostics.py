# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings → Maintenance → Diagnostics: the host checks - the CPU temperature and throttling via
`vcgencmd` (the thermal zone when it is missing). The app adds ffmpeg, library connections and USB devices.

Every check is `{id, label, detail, state}` with state `ok` / `warning` / `error` / `unknown`."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

TEMP_WARN_C = 75.0  # the Pi 5 starts to throttle at 80-85 °C
THERMAL_ZONE = Path("/sys/class/thermal/thermal_zone0/temp")
# get_throttled bits (Raspberry Pi docs): now / since boot
THROTTLE_BITS = {
    0: "under-voltage now",
    1: "ARM frequency capped now",
    2: "throttled now",
    3: "soft temperature limit now",
    16: "under-voltage since boot",
    17: "frequency capped since boot",
    18: "throttled since boot",
    19: "soft temperature limit since boot",
}


def check(id_: str, label: str, detail: str, state: str) -> dict[str, Any]:
    return {"id": id_, "label": label, "detail": detail, "state": state}


def _vcgencmd(*args: str) -> str | None:
    exe = shutil.which("vcgencmd")
    if exe is None:
        return None
    try:
        out = subprocess.run([exe, *args], capture_output=True, text=True, check=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip()


def temperature(run: Any = _vcgencmd, zone: Path = THERMAL_ZONE) -> dict[str, Any]:
    out = run("measure_temp")  # "temp=52.1'C"
    value: float | None = None
    if out and out.startswith("temp="):
        try:
            value = float(out[5:].split("'")[0])
        except ValueError:
            value = None
    if value is None:
        try:
            value = int(zone.read_text().strip()) / 1000
        except (OSError, ValueError):
            return check("temperature", "CPU temperature", "not available on this host", "unknown")
    state = "warning" if value >= TEMP_WARN_C else "ok"
    return check("temperature", "CPU temperature", f"{value:.0f} °C", state)


def throttling(run: Any = _vcgencmd) -> dict[str, Any]:
    out = run("get_throttled")  # "throttled=0x50000"
    if not out or not out.startswith("throttled="):
        why = (
            "no vcgencmd on this host"
            if shutil.which("vcgencmd") is None
            else "vcgencmd failed - the service user needs the video group"
        )
        return check("throttling", "Throttling", f"not available: {why}", "unknown")
    try:
        bits = int(out.split("=", 1)[1], 16)
    except ValueError:
        return check("throttling", "Throttling", out, "unknown")
    if bits == 0:
        return check("throttling", "Throttling", "none since boot (0x0)", "ok")
    seen = [text for bit, text in THROTTLE_BITS.items() if bits & (1 << bit)]
    hint = " - check the power supply" if bits & 0x10001 else ""
    return check("throttling", "Throttling", f"{', '.join(seen)}{hint} ({hex(bits)})", "warning")
