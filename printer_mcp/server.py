"""The MCP server: tools, and the checks that gate them.

Design intent, in one line: read tools are free, mutating tools run the profile's
safety checks first and refuse on failure.

No tool hardcodes a printer, a path, or a temperature. Everything comes from the
loaded profile, so another user can point this at their own machine without
touching code.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from .config import Profile, ProfileError, load_profile
from .moonraker import (
    MoonrakerClient,
    MoonrakerError,
    PrinterUnreachable,
    snapshot_url,
)
from .safety import (
    Check,
    bed_fit_check,
    check_before_start,
    check_filename,
    read_gcode_facts,
)
from .slicer import MeshUnreadable, SliceFailed, measure_stl, slice_model

INSTRUCTIONS = """\
Tools for driving a 3D printer end to end: inspect, slice, upload, start, monitor.

All machine-specific values come from a JSON profile. Call printer_profile first
in a new session to learn which printer, materials and limits are in force.

Mutating tools (upload_and_start, start_print, pause_print, resume_print,
cancel_print) run the profile's safety checks and refuse rather than warn. A
refusal is a real answer, not an obstacle: report it and stop.

Typical sequence:
    printer_profile -> printer_status -> slice_model -> upload_and_start
    -> printer_status until the first layer is down -> printer_snapshot
"""


def build_server(profile: Profile | None = None) -> MCPServer:
    """Construct the server, loading the profile from disk if not supplied."""
    prof = profile or load_profile(required=True)
    assert prof is not None  # load_profile(required=True) raises otherwise

    server = MCPServer(
        "printer-mcp",
        version="0.1.0",
        instructions=INSTRUCTIONS,
    )

    # ---- helpers -----------------------------------------------------

    def client() -> MoonrakerClient:
        return MoonrakerClient(
            host=prof.printer.host, port=prof.printer.moonraker_port
        )

    def live() -> dict:
        """One call for everything the checks need."""
        c = client()
        status = c.query(
            "print_stats", "extruder", "heater_bed", "display_status", "toolhead"
        )
        cfg = c.query("configfile").get("configfile", {}).get("config", {})
        nozzle_raw = (
            cfg.get("extruder", {}).get("nozzle_diameter")
            if isinstance(cfg, dict)
            else None
        )
        nozzle = None
        if isinstance(nozzle_raw, (list, tuple)) and nozzle_raw:
            nozzle = float(nozzle_raw[0])
        elif isinstance(nozzle_raw, str):
            nozzle = float(nozzle_raw)

        return {
            "state": status.get("print_stats", {}).get("state"),
            "bed_temp": status.get("heater_bed", {}).get("temperature"),
            "bed_target": status.get("heater_bed", {}).get("target"),
            "nozzle_temp": status.get("extruder", {}).get("temperature"),
            "nozzle_target": status.get("extruder", {}).get("target"),
            "nozzle_diameter": nozzle,
            "progress": status.get("display_status", {}).get("progress"),
            "position": status.get("toolhead", {}).get("position"),
            "filename": status.get("print_stats", {}).get("filename"),
        }

    def gate(checks) -> tuple[bool, list[dict]]:
        """Split checks into pass/fail. Errors block, warnings pass."""
        dicts = [c.as_dict() for c in checks]
        blocked = [c for c in checks if not c.ok and c.severity == "error"]
        return (not blocked), dicts

    def state_checks(state: dict) -> list[Check]:
        """Checks that need only live printer state."""
        checks: list[Check] = []
        lim = prof.safety

        if lim.refuse_while_printing:
            st = state.get("state")
            idle = st in ("standby", "complete", "cancelled", "error")
            checks.append(
                Check(
                    "printer_idle",
                    idle,
                    f"printer state is '{st}'"
                    + ("" if idle else ", cancel or wait before starting"),
                )
            )

        if (bed := state.get("bed_temp")) is not None:
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

        if lim.verify_live_nozzle and (ln := state.get("nozzle_diameter")):
            match = abs(ln - prof.nozzle_diameter) < 1e-6
            checks.append(
                Check(
                    "nozzle_matches_live_machine",
                    match,
                    f"live nozzle {ln} mm vs profile "
                    f"{prof.nozzle_diameter} mm",
                )
            )

        return checks

    # ---- read-only tools --------------------------------------------

    @server.tool(
        description="Show the loaded profile: printer, materials, limits, slicer profiles."
    )
    def printer_profile() -> dict:
        return {
            "name": prof.name,
            "source": str(prof.source_path) if prof.source_path else "<inline>",
            "printer": {
                "host": prof.printer.host,
                "moonraker_port": prof.printer.moonraker_port,
                "camera": snapshot_url(
                    prof.printer.host,
                    prof.printer.camera_port,
                    prof.printer.camera_snapshot_path,
                ),
                "mac": prof.printer.mac,
            },
            "machine": {
                "nozzle_diameter_mm": prof.nozzle_diameter,
                "build_volume_mm": list(prof.build_volume),
            },
            "slicer": {
                "binary": prof.slicer.binary,
                "machine_profile": prof.slicer.machine_profile,
                "profiles_dir": str(prof.slicer.profiles_dir)
                if prof.slicer.profiles_dir
                else None,
            },
            "materials": {
                k: {
                    "name": m.name,
                    "bed_temp": m.bed_temp,
                    "nozzle_temp": m.nozzle_temp,
                    "max_volumetric_speed": m.max_volumetric_speed,
                }
                for k, m in prof.materials.items()
            },
            "process_profiles": prof.process_profiles,
            "safety": {
                "max_print_time_s": prof.safety.max_print_time_s,
                "max_filament_cm3": prof.safety.max_filament_cm3,
                "max_bed_temp_to_start": prof.safety.max_bed_temp_to_start,
                "refuse_while_printing": prof.safety.refuse_while_printing,
                "enforce_nozzle_match": prof.safety.enforce_nozzle_match,
                "verify_live_nozzle": prof.safety.verify_live_nozzle,
            },
            "notes": prof.notes,
        }

    @server.tool(
        description="Live printer state: temps, progress, job, position."
    )
    def printer_status() -> dict:
        try:
            return {"ok": True, "printer": live()}
        except (PrinterUnreachable, MoonrakerError) as exc:
            return {"ok": False, "error": str(exc)}

    @server.tool(description="List gcode files already on the printer.")
    def printer_files() -> dict:
        try:
            files = client().list_files()
            return {
                "ok": True,
                "count": len(files),
                "files": [
                    {"name": f.get("path"), "size": f.get("size")} for f in files
                ],
            }
        except (PrinterUnreachable, MoonrakerError) as exc:
            return {"ok": False, "error": str(exc)}

    @server.tool(
        description=(
            "Read a gcode file's declared time, filament, nozzle and completeness "
            "without printing it."
        )
    )
    def inspect_gcode(gcode_path: str) -> dict:
        facts = read_gcode_facts(gcode_path)
        nozzle_ok = (
            facts.nozzle_diameter is not None
            and abs(facts.nozzle_diameter - prof.nozzle_diameter) < 1e-6
        )
        return {
            "facts": facts.as_dict(),
            "nozzle_matches_profile": nozzle_ok,
            "profile_nozzle_diameter": prof.nozzle_diameter,
        }

    @server.tool(
        description="Measure a model's bounding box in mm and check it fits the bed."
    )
    def measure_model(model_path: str) -> dict:
        try:
            dims = measure_stl(model_path)
        except MeshUnreadable as exc:
            return {"ok": False, "error": str(exc)}
        check = bed_fit_check(dimensions=dims, build_volume=prof.build_volume)
        return {
            "ok": True,
            "dimensions_mm": {
                "x": round(dims[0], 2),
                "y": round(dims[1], 2),
                "z": round(dims[2], 2),
            },
            "bed_fit": check.as_dict(),
        }

    @server.tool(
        description=(
            "Grab a camera snapshot. Returns the saved path, or the URL when "
            "saving is off."
        )
    )
    def printer_snapshot(save_to: str | None = None) -> dict:
        url = snapshot_url(
            prof.printer.host,
            prof.printer.camera_port,
            prof.printer.camera_snapshot_path,
        )
        if url is None:
            return {
                "ok": False,
                "error": "camera disabled in profile (camera_port is null)",
            }
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = resp.read()
        except Exception as exc:  # noqa: BLE001 - surface any transport failure
            return {"ok": False, "error": f"snapshot failed: {exc}", "url": url}

        if save_to is None:
            return {"ok": True, "url": url, "bytes": len(data), "saved_to": None}

        dest = Path(save_to).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return {
            "ok": True,
            "url": url,
            "bytes": len(data),
            "saved_to": str(dest),
        }

    # ---- slicing (writes a file, moves no hardware) ------------------

    @server.tool(
        description=(
            "Slice a model using a profile's process and material. Returns the "
            "gcode path plus declared facts."
        )
    )
    def slice_model_tool(
        model_path: str,
        process_profile: str,
        material: str,
    ) -> dict:
        mat = prof.materials.get(material)
        if mat is None:
            return {
                "ok": False,
                "error": f"unknown material '{material}'",
                "known_materials": sorted(prof.materials),
            }

        proc_path = prof.process_profiles.get(process_profile)
        if proc_path is None:
            return {
                "ok": False,
                "error": f"unknown process profile '{process_profile}'",
                "known_processes": sorted(prof.process_profiles),
            }

        filament = mat.filament_profiles[0] if mat.filament_profiles else None
        if filament is None:
            return {
                "ok": False,
                "error": f"material '{material}' declares no filament_profiles",
            }

        try:
            result = slice_model(
                slicer=prof.slicer,
                model_path=model_path,
                process_profile=proc_path,
                filament_profile=filament,
            )
        except SliceFailed as exc:
            return {"ok": False, "error": str(exc)}

        facts = read_gcode_facts(result.gcode_path)
        return {
            "ok": not facts.problems,
            "gcode_path": str(result.gcode_path),
            "facts": facts.as_dict(),
            "problems": facts.problems,
        }

    # ---- mutating tools ---------------------------------------------

    @server.tool(
        description=(
            "Upload a gcode file without starting it. Runs filename and nozzle checks."
        )
    )
    def upload_gcode(gcode_path: str, remote_name: str | None = None) -> dict:
        facts = read_gcode_facts(gcode_path)
        name = remote_name or Path(gcode_path).name
        checks = [
            check_filename(name, enforce=prof.safety.enforce_safe_filenames)
        ]
        if facts.nozzle_diameter is not None and prof.safety.enforce_nozzle_match:
            ok = abs(facts.nozzle_diameter - prof.nozzle_diameter) < 1e-6
            checks.append(
                Check(
                    "nozzle_matches_profile",
                    ok,
                    f"gcode {facts.nozzle_diameter} mm vs profile "
                    f"{prof.nozzle_diameter} mm",
                )
            )
        passed, details = gate(checks)
        if not passed:
            return {"ok": False, "refused": True, "checks": details}

        try:
            res = client().upload(gcode_path, name)
        except (PrinterUnreachable, MoonrakerError) as exc:
            return {"ok": False, "error": str(exc), "checks": details}

        return {
            "ok": True,
            "uploaded_as": name,
            "size": (res.get("item") or {}).get("size"),
            "checks": details,
            "facts": facts.as_dict(),
        }

    @server.tool(
        description=(
            "Start printing a gcode file already on the printer. Runs every safety "
            "check and refuses on failure."
        )
    )
    def start_print(filename: str) -> dict:
        try:
            state = live()
        except (PrinterUnreachable, MoonrakerError) as exc:
            return {"ok": False, "error": str(exc)}

        try:
            files = client().list_files()
        except (PrinterUnreachable, MoonrakerError) as exc:
            return {"ok": False, "error": str(exc)}

        remote = next((f for f in files if f.get("path") == filename), None)
        if remote is None:
            return {
                "ok": False,
                "error": f"'{filename}' is not on the printer",
                "available": [f.get("path") for f in files][:20],
            }

        checks = [
            check_filename(filename, enforce=prof.safety.enforce_safe_filenames),
            *state_checks(state),
        ]
        passed, details = gate(checks)
        if not passed:
            return {
                "ok": False,
                "refused": True,
                "reason": "safety checks failed; nothing was started",
                "checks": details,
            }

        try:
            client().start_print(filename)
        except (PrinterUnreachable, MoonrakerError) as exc:
            return {"ok": False, "error": str(exc)}

        return {"ok": True, "started": filename, "checks": details}

    @server.tool(
        description=(
            "Slice, upload and start in one call. Refuses if any check fails."
        )
    )
    def upload_and_start(
        model_path: str,
        process_profile: str,
        material: str,
        remote_name: str | None = None,
    ) -> dict:
        sliced = slice_model_tool(
            model_path=model_path,
            process_profile=process_profile,
            material=material,
        )
        if not sliced.get("ok"):
            return {"ok": False, "stage": "slice", "detail": sliced}

        gcode_path = sliced["gcode_path"]
        facts = read_gcode_facts(gcode_path)
        name = remote_name or Path(gcode_path).name

        try:
            state = live()
        except (PrinterUnreachable, MoonrakerError) as exc:
            return {"ok": False, "stage": "preflight", "error": str(exc)}

        checks = [
            *check_before_start(
                facts=facts,
                profile=prof,
                live_state=state,
                live_nozzle=state.get("nozzle_diameter"),
            ),
            check_filename(name, enforce=prof.safety.enforce_safe_filenames),
        ]

        passed, details = gate(checks)
        if not passed:
            return {
                "ok": False,
                "stage": "preflight",
                "refused": True,
                "reason": "safety checks failed; nothing was uploaded or started",
                "checks": details,
                "facts": facts.as_dict(),
            }

        try:
            client().upload(gcode_path, name)
        except (PrinterUnreachable, MoonrakerError) as exc:
            return {"ok": False, "stage": "upload", "error": str(exc)}

        try:
            client().start_print(name)
        except (PrinterUnreachable, MoonrakerError) as exc:
            return {
                "ok": False,
                "stage": "start",
                "error": str(exc),
                "note": f"{name} was uploaded but not started",
            }

        return {
            "ok": True,
            "stage": "printing",
            "filename": name,
            "gcode_path": gcode_path,
            "checks": details,
            "facts": facts.as_dict(),
        }

    @server.tool(description="Pause the running print.")
    def pause_print() -> dict:
        try:
            client().pause()
            return {"ok": True, "state": live().get("state")}
        except (PrinterUnreachable, MoonrakerError) as exc:
            return {"ok": False, "error": str(exc)}

    @server.tool(description="Resume a paused print.")
    def resume_print() -> dict:
        try:
            client().resume()
            return {"ok": True, "state": live().get("state")}
        except (PrinterUnreachable, MoonrakerError) as exc:
            return {"ok": False, "error": str(exc)}

    @server.tool(
        description="Cancel the running print. Hardware stops and the job is discarded."
    )
    def cancel_print() -> dict:
        try:
            client().cancel()
            return {"ok": True, "state": live().get("state")}
        except (PrinterUnreachable, MoonrakerError) as exc:
            return {"ok": False, "error": str(exc)}

    return server


def main(argv: list[str] | None = None) -> None:
    """Entry point. Transport is stdio by default."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="printer-mcp",
        description="MCP server for driving a Moonraker/Klipper 3D printer.",
    )
    parser.add_argument(
        "--profile",
        help="path to profile JSON (overrides $PRINTER_MCP_PROFILE and defaults)",
    )
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "sse", "streamable-http"],
        help="MCP transport (default: stdio)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the profile and exit without serving",
    )
    args = parser.parse_args(argv)

    try:
        prof = load_profile(args.profile, required=True)
    except ProfileError as exc:
        print(f"printer-mcp: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    assert prof is not None

    if args.check:
        print(
            json.dumps(
                {
                    "profile": prof.name,
                    "source": str(prof.source_path)
                    if prof.source_path
                    else "<inline>",
                    "printer": prof.printer.host,
                    "nozzle_diameter": prof.nozzle_diameter,
                    "materials": sorted(prof.materials),
                    "process_profiles": sorted(prof.process_profiles),
                    "ok": True,
                },
                indent=2,
            )
        )
        return

    build_server(prof).run(transport=args.transport)


if __name__ == "__main__":
    main()
