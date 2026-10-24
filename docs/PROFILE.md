# Profile reference

One JSON file describes a printer. The MCP reads nothing else about the machine.

A profile is the whole customisation surface. Two users with different hardware,
different slicers, and different tolerances share one codebase and differ only here.

## Resolution order

The first file that exists wins:

1. `$PRINTER_MCP_PROFILE`
2. `--profile <path>`
3. `~/.config/printer-mcp/profile.json`
4. `./printer-profile.json`

`~` expands. A relative path resolves against the working directory.

## Top-level keys

| Key | Required | Purpose |
|---|---|---|
| `name` | no | label shown by `printer_profile` |
| `description` | no | free text, unused by code |
| `printer` | **yes** | how to reach the machine |
| `machine` | **yes** | nozzle and build volume |
| `slicer` | **yes** | how to invoke the slicer |
| `materials` | no | named materials, keyed by your own short name |
| `process_profiles` | no | named process profiles, keyed by your own short name |
| `safety` | no | limits the MCP enforces |
| `notes` | no | strings surfaced to the agent |

Keys the code does not recognize are ignored, so you can annotate freely.

## `printer`

| Field | Default | Meaning |
|---|---|---|
| `host` | required | IP or hostname |
| `moonraker_port` | `7125` | Moonraker REST port |
| `upload_port` | `80` | Creality stock upload port, unused on Moonraker |
| `camera_port` | `8080` | MJPEG camera. Set `null` to disable snapshots |
| `camera_snapshot_path` | `/?action=snapshot` | snapshot path |
| `camera_stream_path` | `/?action=stream` | stream path |
| `mac` | `null` | used for ARP discovery when the IP is stale |
| `interface` | `null` | host interface for ARP scans and alias addresses |
| `alias_addresses` | `[]` | CIDR list added to the interface at start |

`alias_addresses` solves a specific and confusing failure. When the printer holds a
static IP on a different subnet than the host, the host routes that traffic to its
default gateway, so the printer looks offline while being physically present. Adding
an address on the printer's subnet makes it on-link. This needs root, so the MCP
does not apply it. The field records the intent for your own setup script.

## `machine`

| Field | Required | Meaning |
|---|---|---|
| `nozzle_diameter` | **yes** | mm. Every nozzle check measures against this |
| `build_volume` | **yes** | `[x, y, z]` in mm |

## `slicer`

| Field | Required | Meaning |
|---|---|---|
| `binary` | **yes** | path or command name |
| `env` | no | environment overrides for the slice process |
| `machine_profile` | no | machine JSON, joined into `--load-settings` |
| `profiles_dir` | no | for your own discovery, unused when profiles are absolute |
| `output_dir` | no | for your own discovery |
| `extra_args` | no | appended to every slice invocation |
| `timeout_s` | `900` | kill the slicer after this many seconds |

`env` exists because an AppImage often needs `LD_LIBRARY_PATH` pointing at its own
runtime, and that differs per install.

## `materials`

Each key is a name you choose. That key is what an agent passes to
`slice_model_tool` and `upload_and_start`.

| Field | Required | Meaning |
|---|---|---|
| `name` | no | display label |
| `bed_temp` | **yes** | °C |
| `nozzle_temp` | **yes** | °C |
| `max_volumetric_speed` | no | mm³/s, recorded for reference |
| `filament_profiles` | no | slicer filament JSON paths. First entry is used |

`bed_temp` and `nozzle_temp` are documentation for the agent. The values that
actually reach the gcode come from the slicer's filament profile. Keep them in sync
by hand, or set the temperatures inside the filament profile itself.

## `process_profiles`

Map of short name to slicer process JSON path. An agent passes the short name.

```json
"process_profiles": {
  "fast": "/path/to/0.28mm-fast.json",
  "detail": "/path/to/0.12mm-detail.json"
}
```

## `safety`

These are the refusals. Tighten them for an unattended setup.

| Field | Default | Meaning |
|---|---|---|
| `max_print_time_s` | `28800` | refuse jobs longer than this |
| `max_filament_cm3` | `300` | refuse jobs using more than this |
| `max_bed_temp_to_start` | `45` | refuse to start above this bed temp |
| `refuse_while_printing` | `true` | refuse to start over a running job |
| `enforce_nozzle_match` | `true` | gcode nozzle must equal profile nozzle |
| `verify_live_nozzle` | `true` | profile nozzle must equal the machine's |
| `enforce_safe_filenames` | `true` | refuse non-ASCII filenames |

`max_bed_temp_to_start` is the one that earns its keep. A finished part sits on a
bed that reads 70 °C, and starting another job behind it crashes the head into the
old part. At 45 °C a PETG bed has cooled enough that the plate is clearable.

## `notes`

Free strings. `printer_profile` returns them, so an agent reads your machine's real
quirks on its first call instead of rediscovering them.

## A complete example

See `profile.example.json` in the repository root. It targets a Creality Ender 3 V3
KE with OrcaSlicer 2.3.2, and every value in it came from a real session. Every
path in it uses `~`, so replace each one with your own layout.

Every path field expands `~` at load time: `slicer.binary`,
`slicer.machine_profile`, `slicer.profiles_dir`, `slicer.output_dir`,
each `materials.*.filament_profiles` entry, and each `process_profiles` value.
Write `~/3dprint/profiles/fast.json` and the loader hands the slicer an absolute
path for the user running the server.

## Validating

```bash
python -m printer_mcp.server --profile ./profile.json --check
```

Prints the parsed values and exits without serving. Errors name the offending key
and its parent object.

## Errors you will hit

`no printer profile found` — none of the four locations exist. Set `PRINTER_MCP_PROFILE`.

`missing required key 'machine'` — the profile is incomplete. Every required key is listed above.

`build_volume must be [x, y, z] in mm` — a two-element list, or a string.

`unknown material 'x'` — the agent asked for a key that is not in `materials`. The
error lists what the profile does define.
