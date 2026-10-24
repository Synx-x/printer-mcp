# AGENTS.md

Guidance for an AI agent working in this repository. `README.md` serves the
human reader and `docs/PROFILE.md` is the schema reference.

## What this is

An MCP server that drives a Moonraker/Klipper 3D printer. Every machine-specific
fact lives in one JSON profile. No module names a printer, a path, or a
temperature.

## Run the tests before claiming a change works

```bash
uv venv .venv
uv pip install --python .venv/bin/python mcp pytest -e .
.venv/bin/python -m pytest tests/ -q
```

31 tests need no printer. A lower number is a failure, not a shorter run.

## The one invariant

Every check lives in `printer_mcp/safety.py`. A check returns a `Check` with a
name, an `ok` flag and a measured value in `detail`. A check never raises,
because the agent reads `detail` to fix the job. A check run collects every
failure in one pass, so the caller sees all of them at once rather than the
first.

## Where things go

- `printer_mcp/config.py` — profile parsing and path resolution
- `printer_mcp/safety.py` — pre-flight checks, the reason this is an MCP
- `printer_mcp/moonraker.py` — thin REST client, no global state
- `printer_mcp/slicer.py` — subprocess slicer call, STL measurement
- `printer_mcp/server.py` — tool definitions and the gate wrapper

## Two portability rules

- An example never carries a machine-local absolute path. An example uses `~` or
  a placeholder instead. The profile loader expands `~`, so an example written
  that way works on the reader's machine.
- An OrcaSlicer slice carries its metadata block after the executable block, near
  EOF. A parser that reads only the first 64 KB finds nothing on a real slice.
  `read_gcode_facts` merges a head read with a tail read.

## Do not weaken a check to make a test pass

A safety check exists because a misconfigured print damages hardware or wastes a
spool. A failing test that contradicts a check is wrong, unless the check's
premise is provably wrong. State which premise and why before touching either.
