"""Pre-flight checks.

An agent will send a 0.2 mm slice to a 0.4 mm machine, or start a print onto a
bed with a finished part still on it. These checks are the reason this is an MCP
rather than a pile of scripts: they run in the tool call, before hardware moves,
and they refuse rather than warn.

Every check returns a Check with ok/severity so the caller can report the whole
picture in one pass instead of failing on the first problem.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# OrcaSlicer 2.3.2 writes its metadata block AFTER the executable block, near the
# end of the file, not in a leading header. Scanning only the first 64 KB finds
# nothing on a real slice. Scan the head and the tail, and merge.
GCODE_HEADER_SCAN_BYTES = 65536
GCODE_TAIL_SCAN_BYTES = 512 * 1024

TIME_RE = re.compile(
    r"^;\s*estimated printing time \(normal mode\)\s*=\s*(.+)$", re.MULTILINE
)
FILAMENT_CM3_RE = re.compile(
    r"^;\s*filament used \[cm3\]\s*=\s*([0-9.]+)", re.MULTILINE
)
NOZZLE_RE = re.compile(r"^;\s*nozzle_diameter\s*=\s*([0-9.]+)", re.MULTILINE)
LAYER_HEIGHT_RE = re.compile(r"^;\s*layer_height\s*=\s*([0-9.]+)", re.MULTILINE)
BED_TEMP_RE = re.compile(r"^M140 S([0-9.]+)", re.MULTILINE)
END_MARKER_RE = re.compile(r"^;\s*EXECUTABLE_BLOCK_END", re.MULTILINE)


def parse_duration(text: str) -> int | None:
    """Parse OrcaSlicer's '1h 2m 31s' style into seconds."""
    total = 0
    found = False
    for value, unit in re.findall(r"(\d+)\s*([hms])", text):
        found = True
        n = int(value)
        total += n * {"h": 3600, "m": 60, "s": 1}[unit]
    return total if found else None


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    severity: str = "error"  # error | warning

    def as_dict(self) -> dict:
        return {
            "check": self.name,
            "ok": self.ok,
            "severity": self.severity,
            "detail": self.detail,
        }


@dataclass
class GcodeFacts:
    """What the gcode claims about itself. Parsed, never assumed."""

    path: Path
    size_bytes: int = 0
    estimated_seconds: int | None = None
    estimated_time_text: str | None = None
    filament_cm3: float | None = None
    nozzle_diameter: float | None = None
    layer_height: float | None = None
    bed_temp: float | None = None
    complete: bool = False
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "path": str(self.path),
            "size_bytes": self.size_bytes,
            "estimated_seconds": self.estimated_seconds,
            "estimated_time_text": self.estimated_time_text,
            "filament_cm3": self.filament_cm3,
            "nozzle_diameter": self.nozzle_diameter,
            "layer_height": self.layer_height,
            "bed_temp": self.bed_temp,
            "complete": self.complete,
            "problems": self.problems,
        }


def read_gcode_facts(path: str | Path) -> GcodeFacts:
    """Parse the header and confirm the file ends properly.

    A truncated gcode stops mid-move and leaves the print hanging. The header
    alone cannot detect that, so the tail is checked for the end marker.
    """
    p = Path(path)
    facts = GcodeFacts(path=p)

    if not p.is_file():
        facts.problems.append(f"file not found: {p}")
        return facts

    facts.size_bytes = p.stat().st_size
    if facts.size_bytes == 0:
        facts.problems.append("file is empty")
        return facts

    with p.open("rb") as fh:
        head = fh.read(GCODE_HEADER_SCAN_BYTES).decode("utf-8", errors="replace")

    # Metadata may live in the head (older slicers) or the tail (OrcaSlicer 2.3.x
    # writes its CONFIG_BLOCK after the executable block). Search both.
    tail_len = min(facts.size_bytes, GCODE_TAIL_SCAN_BYTES)
    with p.open("rb") as fh:
        fh.seek(max(0, facts.size_bytes - tail_len))
        tail = fh.read().decode("utf-8", errors="replace")

    searchable = head + "\n" + tail

    if m := TIME_RE.search(searchable):
        facts.estimated_time_text = m.group(1).strip()
        facts.estimated_seconds = parse_duration(facts.estimated_time_text)
        if facts.estimated_seconds is None:
            facts.problems.append(
                f"could not parse estimated time: {facts.estimated_time_text!r}"
            )
    else:
        facts.problems.append(
            "no 'estimated printing time' metadata; this may not be sliced by "
            "OrcaSlicer or may be a hand-edited file"
        )

    if m := FILAMENT_CM3_RE.search(searchable):
        facts.filament_cm3 = float(m.group(1))

    if m := NOZZLE_RE.search(searchable):
        facts.nozzle_diameter = float(m.group(1))

    if m := LAYER_HEIGHT_RE.search(searchable):
        facts.layer_height = float(m.group(1))

    if m := BED_TEMP_RE.search(head):
        facts.bed_temp = float(m.group(1))

    facts.complete = bool(END_MARKER_RE.search(searchable))
    if not facts.complete:
        facts.problems.append(
            "no EXECUTABLE_BLOCK_END marker in head or tail; file may be truncated"
        )

    return facts


def check_before_start(
    *,
    facts: GcodeFacts,
    profile,
    live_state: dict | None,
    live_nozzle: float | None,
) -> list[Check]:
    """Everything that must hold before a print is allowed to start."""
    checks: list[Check] = []
    lim = profile.safety

    # 1. The file must be complete.
    checks.append(
        Check(
            "gcode_complete",
            facts.complete,
            "end marker present"
            if facts.complete
            else "missing EXECUTABLE_BLOCK_END, refusing a possibly truncated file",
        )
    )

    # 2. Declared print time within the limit.
    if facts.estimated_seconds is None:
        checks.append(
            Check(
                "print_time_within_limit",
                False,
                "cannot determine print duration, refusing",
            )
        )
    else:
        within = facts.estimated_seconds <= lim.max_print_time_s
        checks.append(
            Check(
                "print_time_within_limit",
                within,
                f"{facts.estimated_time_text} vs limit "
                f"{lim.max_print_time_s // 60} min",
            )
        )

    # 3. Filament budget.
    if facts.filament_cm3 is not None:
        within = facts.filament_cm3 <= lim.max_filament_cm3
        checks.append(
            Check(
                "filament_within_limit",
                within,
                f"{facts.filament_cm3:.2f} cm3 vs limit {lim.max_filament_cm3} cm3",
            )
        )

    # 4. Nozzle match against the profile.
    if lim.enforce_nozzle_match:
        if facts.nozzle_diameter is None:
            checks.append(
                Check(
                    "nozzle_matches_profile",
                    False,
                    "gcode does not declare nozzle_diameter, refusing",
                )
            )
        else:
            match = abs(facts.nozzle_diameter - profile.nozzle_diameter) < 1e-6
            checks.append(
                Check(
                    "nozzle_matches_profile",
                    match,
                    f"gcode {facts.nozzle_diameter} mm vs profile "
                    f"{profile.nozzle_diameter} mm",
                )
            )

    # 5. Nozzle match against the live machine.
    if lim.verify_live_nozzle and live_nozzle is not None:
        match = abs(live_nozzle - profile.nozzle_diameter) < 1e-6
        checks.append(
            Check(
                "nozzle_matches_live_machine",
                match,
                f"live nozzle {live_nozzle} mm vs profile "
                f"{profile.nozzle_diameter} mm",
            )
        )

    # 6. Printer idle.
    if lim.refuse_while_printing and live_state is not None:
        state = live_state.get("state", "unknown")
        idle = state in ("standby", "complete", "cancelled", "error")
        checks.append(
            Check(
                "printer_idle",
                idle,
                f"printer state is '{state}'"
                + ("" if idle else ", cancel or wait before starting"),
            )
        )

    # 7. Bed cool enough that a finished part is not sitting on it.
    if live_state is not None:
        bed = live_state.get("bed_temp")
        if bed is not None:
            cool = bed <= lim.max_bed_temp_to_start
            checks.append(
                Check(
                    "bed_cool",
                    cool,
                    f"bed at {bed:.1f} C vs start limit "
                    f"{lim.max_bed_temp_to_start} C"
                    + ("" if cool else ", let it cool and clear the plate"),
                )
            )

    return checks


def check_filename(name: str, *, enforce: bool) -> Check:
    """Firmware-safe filename check. Refuses rather than silently rewriting."""
    from .config import SAFE_NAME

    ok = bool(SAFE_NAME.match(name))
    if not ok and not enforce:
        return Check("filename_safe", True, f"'{name}' unusual but not enforced")
    return Check(
        "filename_safe",
        ok,
        f"'{name}' is ASCII-safe"
        if ok
        else f"'{name}' has characters the firmware mishandles; use [A-Za-z0-9._-]",
    )


def bed_fit_check(
    *, dimensions: tuple[float, float, float], build_volume: tuple[float, float, float]
) -> Check:
    """Model must fit the declared build volume, with nothing to spare."""
    x, y, z = dimensions
    bx, by, bz = build_volume
    fits = x <= bx and y <= by and z <= bz
    return Check(
        "fits_build_volume",
        fits,
        f"model {x:.1f}x{y:.1f}x{z:.1f} mm vs bed {bx:.0f}x{by:.0f}x{bz:.0f} mm",
    )
