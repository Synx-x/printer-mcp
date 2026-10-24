# printer-mcp

An MCP server that drives a Moonraker/Klipper 3D printer end to end: inspect,
slice, upload, start, monitor. Every machine-specific value lives in one JSON
profile, so pointing it at a different printer is a config change, not a code
change.

Built and verified against a Creality Ender 3 V3 KE on Klipper + Moonraker with
OrcaSlicer 2.3.2. Nothing in the code names that printer.

## Install

```bash
git clone https://github.com/Synx-x/printer-mcp
cd printer-mcp
uv venv .venv
uv pip install --python .venv/bin/python mcp -e .
```

Python 3.11 or newer. The only runtime dependency is `mcp`.

## Configure

Copy `profile.example.json` to `~/.config/printer-mcp/profile.json` and replace
every path with your own. `~` expands, so a profile written that way works on a
different machine.

Resolution order, first existing file wins:

1. `$PRINTER_MCP_PROFILE`
2. `--profile <path>`
3. `~/.config/printer-mcp/profile.json`
4. `./printer-profile.json`

Validate without serving:

```bash
.venv/bin/python -m printer_mcp.server --check
```

That prints the parsed values and exits. A bad key names the offending field and
its parent object.

## Register with an agent

```bash
claude mcp add printer --scope user -- \
  /absolute/path/to/.venv/bin/python -m printer_mcp.server
```

`--scope user` makes it available in every project. Confirm the connection:

```bash
claude mcp list | grep printer
```

## Tools

Read-only, always safe:

| Tool | Returns |
|---|---|
| `printer_profile` | loaded config: printer, materials, limits, process profiles |
| `printer_status` | live state, temps, progress, job name, position |
| `printer_files` | gcode already on the printer |
| `inspect_gcode` | declared time, filament, nozzle, completeness |
| `measure_model` | bounding box in mm plus a bed-fit verdict |
| `printer_snapshot` | camera JPEG path |

Mutating, gated by the profile's safety checks:

| Tool | Gate |
|---|---|
| `slice_model_tool` | process and material must exist in the profile |
| `upload_gcode` | filename safety, nozzle match |
| `start_print` | completeness, time, filament, nozzle, printer idle, bed cool |
| `upload_and_start` | all of the above in one call |
| `pause_print` / `resume_print` / `cancel_print` | none, always allowed |

`slice_model_tool` takes `process_profile`, not `process`.

## Why an MCP and not a skill or a pile of scripts

Scripts already existed for this pipeline: `slice.sh`, `upload_and_print.py` and
`start_print.py`. Scripts are advisory, so a model reads one and may skip a step.
Scripts also cannot stop a failure inside a call, and two such failures happened
on 2026-10-24. Scripts let a print start while a finished part sat on a bed at
69 °C, and a 0.2 mm slice nearly went to a 0.4 mm machine.

An MCP runs the check inside the tool call, so the refusal is a return value
rather than an instruction, and the agent cannot talk past it.

| Concern | Skill | Script | This MCP |
|---|---|---|---|
| Agent can skip it | yes | yes | no, the tool refuses |
| Checks run before hardware | no | no | yes |
| One interface for any agent | no | no | yes |
| Per-user config | prose | edit code | one JSON file |

## The safety checks

Each check returns a named pass or refusal with a measured value, so a blocked
run tells the agent exactly what to change.

- `gcode_complete` — an `EXECUTABLE_BLOCK_END` marker exists. A truncated file hangs mid-move.
- `print_time_within_limit` — declared duration against `max_print_time_s`.
- `filament_within_limit` — declared volume against `max_filament_cm3`.
- `nozzle_matches_profile` — the gcode's declared nozzle against the profile.
- `nozzle_matches_live_machine` — the profile against the printer's actual config.
- `printer_idle` — refuses to start over a running job.
- `bed_cool` — refuses to start onto a hot bed, where a finished part is sitting.
- `filename_safe` — firmware mangles spaces and unicode in gcode names.
- `fits_build_volume` — model dimensions against the declared volume.

## Profile schema

Full reference in `docs/PROFILE.md`. The short version:

```json
{
  "printer":   { "host": "192.168.1.200", "moonraker_port": 7125 },
  "machine":   { "nozzle_diameter": 0.4, "build_volume": [220, 220, 245] },
  "slicer":    { "binary": "~/Apps/orca-slicer.AppImage",
                 "machine_profile": "~/.config/OrcaSlicer/machine.json" },
  "materials": { "pla": { "bed_temp": 60, "nozzle_temp": 215,
                          "filament_profiles": ["~/profiles/pla.json"] } },
  "process_profiles": { "fast": "~/profiles/fast-020.json" },
  "safety":    { "max_print_time_s": 28800, "max_bed_temp_to_start": 45 }
}
```

## Gotchas this encodes

Each of these was a real failure on the reference machine, not theory.

- **Metadata sits at the end.** OrcaSlicer 2.3.2 writes its config block after the
  executable block, near EOF. Scanning the first 64 KB of a real slice finds
  nothing. The parser reads the head and the tail and merges them.
- **The printer ignores ICMP.** `ping` fails while HTTP works. Test reachability
  over TCP.
- **A static IP on another subnet is invisible to a TCP sweep.** If the printer
  holds `192.168.1.x` and the host sits on `192.168.2.x`, the host routes the
  traffic to its ISP gateway and the printer looks offline. A raw ARP scan finds
  it by MAC while TCP and ICMP do not. The profile's `alias_addresses` records
  the host-side fix, which needs root, so the MCP does not apply it.
- **Stock profiles carry Bambu defaults.** A 35 °C bed on a PEI plate loses a
  three-hour PLA print. Set the bed type and the temperature explicitly.
- **Bambu 2.4 3MF files will not load in OrcaSlicer 2.3.2.** Strip
  `Metadata/*.config` from the archive, or bake the meshes to STL.
- **A long unsupported span fails regardless of speed.** A 155 mm ceiling in PLA
  drapes and cracks. Nozzle and acceleration tuning will not fix it. Flip the
  part or add supports.

## Test

```bash
env -u PYTEST_ADDOPTS .venv/bin/python -m pytest tests/ -q
```

31 tests, no printer needed. The suite runs every safety check against synthetic
gcode.

## Status

- 31 tests passing, last run 2026-10-24.
- Sliced, uploaded and started real prints on an Ender 3 V3 KE, last on 2026-10-24.
- Refusal path verified live against a running job at 70 °C bed.

## Limitations

- Moonraker/Klipper only. A Marlin printer needs a different transport.
- Slicing needs an installed OrcaSlicer or a CLI-compatible fork. The checks work
  without one.
- `measure_model` reads STL. 3MF measurement is not implemented.
- No authentication. Run it on a trusted network.
- `alias_addresses` is a note to your own setup script, not something the server
  applies.

## License

MIT. See `LICENSE`.
