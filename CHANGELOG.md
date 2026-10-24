# Changelog

All notable changes to this project are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-10-24

### Added

- One JSON profile defines the whole machine: host, ports, nozzle, build volume,
  slicer invocation, materials, process profiles and safety limits. Nothing in
  the code names a printer, a path or a temperature.
- Profile resolution across four locations, in order: `$PRINTER_MCP_PROFILE`,
  `--profile`, `~/.config/printer-mcp/profile.json`, `./printer-profile.json`.
  Every path field expands `~` at load time.
- Read-only tools: `printer_profile`, `printer_status`, `printer_files`,
  `inspect_gcode`, `measure_model`, `printer_snapshot`.
- Mutating tools gated by the profile's safety checks: `slice_model_tool`,
  `upload_gcode`, `start_print`, `upload_and_start`, `pause_print`,
  `resume_print`, `cancel_print`.
- Nine pre-flight checks, each returning a named verdict with the measured
  value: `gcode_complete`, `print_time_within_limit`, `filament_within_limit`,
  `nozzle_matches_profile`, `nozzle_matches_live_machine`, `printer_idle`,
  `bed_cool`, `filename_safe`, `fits_build_volume`.
- `--check` prints the parsed profile and exits without serving, so a config
  error names the offending key and its parent object before any tool runs.
- A test suite of 31 cases that exercises every safety check against synthetic
  gcode, with no printer attached.

[0.1.0]: https://github.com/Synx-x/printer-mcp/releases/tag/v0.1.0
