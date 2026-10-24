"""Slicer invocation and mesh measurement.

Two jobs:
  - run the configured slicer binary against profile files
  - read model dimensions so the bed-fit check has real numbers

Mesh measurement reads binary and ASCII STL without a dependency, because the
whole value of a pre-flight check is that it works before anything is installed.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


class SliceFailed(Exception):
    """The slicer exited non-zero or produced no plate."""


class MeshUnreadable(Exception):
    """The model file could not be parsed as STL or 3MF."""


@dataclass
class SliceResult:
    gcode_path: Path
    stdout: str
    stderr: str
    returncode: int


def measure_stl(path: str | Path) -> tuple[float, float, float]:
    """Return the bounding-box size of an STL in mm as (x, y, z)."""
    p = Path(path)
    if not p.is_file():
        raise MeshUnreadable(f"file not found: {p}")

    with p.open("rb") as fh:
        head = fh.read(84)
        if len(head) < 84:
            raise MeshUnreadable(f"{p}: too short to be an STL")

        # Binary STL: 80-byte header, then uint32 triangle count.
        count = struct.unpack("<I", head[80:84])[0]
        expected = 84 + count * 50
        actual = p.stat().st_size

        if expected == actual and count > 0:
            return _measure_binary(fh, count)

        # Fall back to ASCII.
        fh.seek(0)
        try:
            text = fh.read().decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise MeshUnreadable(
                f"{p}: not binary STL (size {actual} != expected {expected}) "
                "and not valid ASCII STL"
            ) from exc

        if "facet" not in text:
            raise MeshUnreadable(
                f"{p}: not binary STL (size mismatch) and no 'facet' keyword in text"
            )
        return _measure_ascii(text)


def _measure_binary(fh, count: int) -> tuple[float, float, float]:
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    fh.seek(84)
    raw = fh.read(count * 50)
    for i in range(count):
        off = i * 50 + 12
        vals = struct.unpack("<9f", raw[off : off + 36])
        for v in range(3):
            for axis in range(3):
                coord = vals[v * 3 + axis]
                if coord < lo[axis]:
                    lo[axis] = coord
                if coord > hi[axis]:
                    hi[axis] = coord
    return (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2])


def _measure_ascii(text: str) -> tuple[float, float, float]:
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    found = False
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("vertex"):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            coords = (float(parts[1]), float(parts[2]), float(parts[3]))
        except ValueError:
            continue
        found = True
        for axis in range(3):
            if coords[axis] < lo[axis]:
                lo[axis] = coords[axis]
            if coords[axis] > hi[axis]:
                hi[axis] = coords[axis]
    if not found:
        raise MeshUnreadable("ASCII STL contains no vertex lines")
    return (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2])


def slice_model(
    *,
    slicer,
    model_path: str | Path,
    process_profile: str | Path,
    filament_profile: str | Path,
) -> SliceResult:
    """Run the slicer and return the produced gcode.

    The output directory is a fresh temp dir, so a stale plate_1.gcode from an
    earlier run cannot be mistaken for this run's result.
    """
    binary = slicer.binary
    if not (Path(binary).is_file() or shutil.which(binary)):
        raise SliceFailed(f"slicer binary not found: {binary}")

    model = Path(model_path).expanduser().resolve()
    if not model.is_file():
        raise SliceFailed(f"model not found: {model}")

    outdir = Path(tempfile.mkdtemp(prefix="printer-mcp-slice-"))

    settings = f"{process_profile}" if isinstance(process_profile, str) else str(process_profile)
    machine = slicer.machine_profile
    load_settings = f"{machine};{settings}" if machine else settings

    cmd = [
        binary,
        "--load-settings",
        load_settings,
        "--load-filaments",
        str(filament_profile),
        "--arrange",
        "1",
        "--slice",
        "0",
        "--outputdir",
        str(outdir),
        *slicer.extra_args,
        str(model),
    ]

    env = {**os.environ, **slicer.env}

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=slicer.timeout_s,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SliceFailed(
            f"slicer exceeded {slicer.timeout_s}s on {model.name}"
        ) from exc

    plates = sorted(outdir.glob("plate_*.gcode"))
    if not plates:
        raise SliceFailed(
            "slicer produced no plate gcode.\n"
            f"exit={proc.returncode}\n"
            f"stdout tail: {proc.stdout[-800:]}\n"
            f"stderr tail: {proc.stderr[-800:]}"
        )

    if len(plates) > 1:
        # Multiple plates means the models did not fit on one bed. Report it
        # rather than silently printing only the first.
        names = ", ".join(p.name for p in plates)
        raise SliceFailed(
            f"slicer produced {len(plates)} plates ({names}). The models do not "
            "fit one bed; slice them separately or reduce the set."
        )

    return SliceResult(
        gcode_path=plates[0],
        stdout=proc.stdout,
        stderr=proc.stderr,
        returncode=proc.returncode,
    )
