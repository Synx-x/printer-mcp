"""Profile loading and validation.

The whole point of this package: every site-specific fact lives in one JSON
profile, and the MCP enforces what the profile declares. Nothing about a
particular printer is hardcoded here.

Profile resolution order:
    1. $PRINTER_MCP_PROFILE
    2. --profile <path> / profile= in constructor
    3. ~/.config/printer-mcp/profile.json
    4. ./printer-profile.json

Schema is documented in profile.schema.json and docs/PROFILE.md.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ProfileError(Exception):
    """The profile is missing, unreadable, or contradicts itself."""


def default_profile_paths() -> tuple[Path, Path]:
    """Resolve the fallback locations at call time.

    `Path.home()` reads $HOME once and caches the answer, so building this tuple
    at import time pins the home directory of whichever process imported first.
    That breaks a server handed a different HOME, and it hides the fallback from
    any test that moves HOME before loading.
    """
    return (
        Path.home() / ".config" / "printer-mcp" / "profile.json",
        Path.cwd() / "printer-profile.json",
    )

# Firmware-compatible gcode filenames. Creality stock firmware stores the file
# under the name in the upload path, and rejects or mangles spaces and unicode.
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def _require(d: dict, key: str, kind: type, where: str) -> Any:
    if key not in d:
        raise ProfileError(f"{where}: missing required key '{key}'")
    val = d[key]
    if not isinstance(val, kind):
        raise ProfileError(
            f"{where}: '{key}' must be {kind.__name__}, got {type(val).__name__}"
        )
    return val


@dataclass
class PrinterEndpoint:
    """How to reach the printer."""

    host: str
    moonraker_port: int = 7125
    upload_port: int | None = 80
    camera_port: int | None = 8080
    camera_snapshot_path: str = "/?action=snapshot"
    camera_stream_path: str = "/?action=stream"
    #: MAC used for ARP-discovery when the static IP is unreachable.
    mac: str | None = None
    #: Extra local addresses to add to the interface so a printer on a
    #: different subnet becomes on-link. Linux only.
    alias_addresses: list[str] = field(default_factory=list)
    interface: str | None = None


@dataclass
class SlicerConfig:
    """Where the slicer binary is and how to call it."""

    binary: str
    env: dict[str, str] = field(default_factory=dict)
    profiles_dir: Path | None = None
    machine_profile: str | None = None
    output_dir: Path | None = None
    #: Extra CLI args appended to every slice call.
    extra_args: list[str] = field(default_factory=list)
    timeout_s: int = 900


@dataclass
class SafetyLimits:
    """Hard refusals the MCP enforces before touching hardware.

    These exist because an agent will happily send a 0.2 mm-nozzle slice to a
    0.4 mm machine, or start a print onto a hot bed with a finished part on it.
    """

    max_print_time_s: int = 8 * 3600
    max_filament_cm3: float = 300.0
    #: Bed must be below this before a new print may start.
    max_bed_temp_to_start: float = 45.0
    #: Refuse to start if a print is already running.
    refuse_while_printing: bool = True
    #: Refuse gcode whose nozzle_diameter differs from the profile.
    enforce_nozzle_match: bool = True
    #: Refuse if the profile's declared nozzle differs from live machine config.
    verify_live_nozzle: bool = True
    #: Refuse uploads with unsafe filenames rather than silently rewriting.
    enforce_safe_filenames: bool = True


@dataclass
class Material:
    """A named material the machine may run."""

    name: str
    bed_temp: float
    nozzle_temp: float
    max_volumetric_speed: float | None = None
    #: Profiles (filament JSON basenames) that use this material.
    filament_profiles: list[str] = field(default_factory=list)


@dataclass
class Profile:
    """A complete site profile."""

    name: str
    source_path: Path | None
    printer: PrinterEndpoint
    slicer: SlicerConfig
    safety: SafetyLimits
    nozzle_diameter: float
    build_volume: tuple[float, float, float]
    materials: dict[str, Material] = field(default_factory=dict)
    #: Named process profiles, keyed by short name.
    process_profiles: dict[str, str] = field(default_factory=dict)
    #: Free-form notes surfaced to the agent in tool descriptions.
    notes: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def material(self, name: str) -> Material:
        try:
            return self.materials[name]
        except KeyError:
            known = ", ".join(sorted(self.materials)) or "(none defined)"
            raise ProfileError(
                f"unknown material '{name}'; profile defines: {known}"
            ) from None


def _parse_profile(data: dict, path: Path | None) -> Profile:
    where = str(path) if path else "<inline>"

    printer_raw = _require(data, "printer", dict, where)
    host = _require(printer_raw, "host", str, f"{where}:printer")

    ep = PrinterEndpoint(
        host=host,
        moonraker_port=printer_raw.get("moonraker_port", 7125),
        upload_port=printer_raw.get("upload_port", 80),
        camera_port=printer_raw.get("camera_port", 8080),
        camera_snapshot_path=printer_raw.get(
            "camera_snapshot_path", "/?action=snapshot"
        ),
        camera_stream_path=printer_raw.get(
            "camera_stream_path", "/?action=stream"
        ),
        mac=printer_raw.get("mac"),
        alias_addresses=list(printer_raw.get("alias_addresses") or []),
        interface=printer_raw.get("interface"),
    )

    slicer_raw = _require(data, "slicer", dict, where)
    binary = _require(slicer_raw, "binary", str, f"{where}:slicer")
    sc = SlicerConfig(
        binary=str(Path(binary).expanduser()) if binary.startswith("~") else binary,
        env=dict(slicer_raw.get("env") or {}),
        profiles_dir=Path(slicer_raw["profiles_dir"]).expanduser()
        if slicer_raw.get("profiles_dir")
        else None,
        machine_profile=str(Path(slicer_raw["machine_profile"]).expanduser())
        if slicer_raw.get("machine_profile")
        else None,
        output_dir=Path(slicer_raw["output_dir"]).expanduser()
        if slicer_raw.get("output_dir")
        else None,
        extra_args=list(slicer_raw.get("extra_args") or []),
        timeout_s=int(slicer_raw.get("timeout_s", 900)),
    )

    safety_raw = data.get("safety") or {}
    sl = SafetyLimits(
        max_print_time_s=int(
            safety_raw.get("max_print_time_s", 8 * 3600)
        ),
        max_filament_cm3=float(safety_raw.get("max_filament_cm3", 300.0)),
        max_bed_temp_to_start=float(
            safety_raw.get("max_bed_temp_to_start", 45.0)
        ),
        refuse_while_printing=bool(
            safety_raw.get("refuse_while_printing", True)
        ),
        enforce_nozzle_match=bool(
            safety_raw.get("enforce_nozzle_match", True)
        ),
        verify_live_nozzle=bool(safety_raw.get("verify_live_nozzle", True)),
        enforce_safe_filenames=bool(
            safety_raw.get("enforce_safe_filenames", True)
        ),
    )

    machine = _require(data, "machine", dict, where)
    nozzle = float(_require(machine, "nozzle_diameter", (int, float), f"{where}:machine"))
    bv = machine.get("build_volume")
    if not (isinstance(bv, list) and len(bv) == 3):
        raise ProfileError(
            f"{where}:machine: 'build_volume' must be [x, y, z] in mm"
        )
    build_volume = (float(bv[0]), float(bv[1]), float(bv[2]))

    mats: dict[str, Material] = {}
    for key, m in (data.get("materials") or {}).items():
        if not isinstance(m, dict):
            raise ProfileError(f"{where}:materials.{key} must be an object")
        mats[key] = Material(
            name=m.get("name", key),
            bed_temp=float(_require(m, "bed_temp", (int, float), f"{where}:materials.{key}")),
            nozzle_temp=float(
                _require(m, "nozzle_temp", (int, float), f"{where}:materials.{key}")
            ),
            max_volumetric_speed=m.get("max_volumetric_speed"),
            filament_profiles=[
                str(Path(p).expanduser()) for p in (m.get("filament_profiles") or [])
            ],
        )

    procs = {
        str(k): str(Path(v).expanduser())
        for k, v in (data.get("process_profiles") or {}).items()
    }

    return Profile(
        name=str(data.get("name", "unnamed")),
        source_path=path,
        printer=ep,
        slicer=sc,
        safety=sl,
        nozzle_diameter=nozzle,
        build_volume=build_volume,
        materials=mats,
        process_profiles=procs,
        notes=list(data.get("notes") or []),
        raw=data,
    )


def load_profile(
    path: str | Path | None = None, *, required: bool = True
) -> Profile | None:
    """Load a profile, or return None when none is found and not required."""
    candidates: list[Path] = []

    env_path = os.environ.get("PRINTER_MCP_PROFILE")
    if env_path:
        candidates.append(Path(env_path).expanduser())

    if path:
        candidates.append(Path(path).expanduser())

    candidates.extend(default_profile_paths())

    for cand in candidates:
        if cand.is_file():
            try:
                data = json.loads(cand.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ProfileError(f"{cand}: invalid JSON: {exc}") from exc
            if not isinstance(data, dict):
                raise ProfileError(f"{cand}: top level must be an object")
            return _parse_profile(data, cand)

    if required:
        tried = "\n  ".join(str(c) for c in candidates)
        raise ProfileError(
            "no printer profile found. Set $PRINTER_MCP_PROFILE or place one at "
            f"~/.config/printer-mcp/profile.json. Tried:\n  {tried}\n"
            "Copy profile.example.json from the package to get started."
        )
    return None
