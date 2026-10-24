# printer-mcp

An MCP server that drives a Moonraker/Klipper 3D printer end to end: inspect,
slice, upload, start, monitor. Every machine-specific value lives in one JSON
profile, so pointing it at a different printer is a config change, not a code
change.

Targets Moonraker and Klipper with OrcaSlicer 2.3.x as the slicer. No module
names a printer, a path, or a temperature.

## What it looks like

```
> printer_status

{ "ok": true,
  "printer": { "state": "printing", "bed_temp": 70.0, "nozzle_temp": 250.0,
               "progress": 0.31, "filename": "bracket-petg.gcode" } }

> slice_model_tool(model_path="bracket.stl",
                   process_profile="petg-020-supported",
                   material="petg-black")

{ "ok": true,
  "gcode_path": "/tmp/printer-mcp-slice-xxxx/plate_1.gcode",
  "facts": { "estimated_time_text": "1h 2m 31s", "filament_cm3": 21.37,
             "nozzle_diameter": 0.4, "complete": true, "problems": [] } }
```

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

## Why an MCP rather than a skill or a shell script

A skill and a shell script both describe work. The agent reads the description
and decides whether to follow it. Nothing in either form can stop a call from
completing.

An MCP puts the check inside the tool call. The gate runs before hardware moves,
and its verdict is a return value, so a refusal is the result of the call rather
than advice about the call. An agent cannot skip it by not reading it.

| Concern | Skill | Script | This MCP |
|---|---|---|---|
| Agent can skip it | yes | yes | no, the tool refuses |
| Checks run before hardware | no | no | yes |
| One interface for any agent | no | no | yes |
| Per-user config | prose | edit code | one JSON file |
| Failure reported as data | no | no | yes, named check plus measured value |

## The safety checks

Each check returns a named pass or refusal with a measured value, so a blocked
run tells the agent exactly what to change.

| Check | Refuses when |
|---|---|
| `gcode_complete` | no `EXECUTABLE_BLOCK_END` marker exists, so a truncated file would hang mid-move |
| `print_time_within_limit` | declared duration exceeds `max_print_time_s` |
| `filament_within_limit` | declared volume exceeds `max_filament_cm3` |
| `nozzle_matches_profile` | the gcode's declared nozzle differs from the profile |
| `nozzle_matches_live_machine` | the profile differs from the printer's actual config |
| `printer_idle` | a job is already running |
| `bed_cool` | the bed is above `max_bed_temp_to_start`, where a finished part is sitting |
| `filename_safe` | the name holds spaces or unicode, which firmware mangles |
| `fits_build_volume` | model dimensions exceed the declared volume |

The two nozzle checks and the bed check cover the failures that ruin hardware
rather than material: a slice made for one nozzle size sent to another, and a
start onto a plate still holding a finished part.

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

## Domain behaviour the design accounts for

Each item below is a property of the pipeline that costs a print when handled
wrong.

- **OrcaSlicer writes its metadata after the executable block.** The config block
  sits near EOF, not in a leading header, so a parser reading the first 64 KB of a
  real slice finds nothing. `read_gcode_facts` reads the head and the tail and
  merges them.
- **The printer ignores ICMP.** `ping` fails while HTTP works. Reachability is a
  TCP test.
- **A static IP on another subnet is invisible to a TCP sweep.** When the printer
  holds an address on a subnet the host is not on, the host routes that traffic to
  its default gateway and the printer looks offline while being physically present.
  A raw ARP scan finds it by MAC. `alias_addresses` records the host-side fix,
  which needs root, so the MCP does not apply it.
- **Stock profiles carry foreign defaults.** A 35 °C bed meant for a cool plate
  loses a PLA print on textured PEI. Set the bed type and temperature explicitly.
- **Newer 3MF archives will not load in an older OrcaSlicer.** A Bambu 2.4 archive
  fails in 2.3.2. Strip `Metadata/*.config` from the archive, or bake the meshes to
  STL.
- **A long unsupported span fails regardless of speed.** A wide ceiling in PLA
  drapes and cracks. Nozzle and acceleration tuning will not fix it. Flip the part
  or add supports.

## Test

```bash
.venv/bin/python -m pytest tests/ -q
```

31 tests, no printer needed. The suite runs every safety check against synthetic
gcode.

## Limitations

- Moonraker/Klipper only. A Marlin printer needs a different transport.
- Slicing needs an installed OrcaSlicer or a CLI-compatible fork. The checks work
  without one.
- `measure_model` reads STL. 3MF measurement is not implemented.
- No authentication. Run it on a trusted network.
- `alias_addresses` documents a host-side setup step. The server does not apply it.

## License

MIT. See `LICENSE`.
