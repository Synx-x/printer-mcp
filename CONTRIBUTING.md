# Contributing

Nothing in this repository names a printer. That is the core design rule.
`printer_mcp` is generic, and every machine fact arrives through one JSON
profile. A default temperature or a default path does not belong in a module. It
belongs in the profile schema.

## Setup

```bash
git clone https://github.com/Synx-x/printer-mcp
cd printer-mcp
uv venv .venv
uv pip install --python .venv/bin/python mcp pytest -e .
```

Copy `profile.example.json` to `~/.config/printer-mcp/profile.json` and edit the
paths. Nothing here runs against real hardware until a profile points at one.

## Verify

```bash
.venv/bin/python -m pytest tests/ -q
```

Expect `31 passed`. The suite needs no printer and no network.

## Run

```bash
.venv/bin/python -m printer_mcp.server --check   # validate the profile, serve nothing
.venv/bin/python -m printer_mcp.server           # serve over stdio
```

## Branching

- Branch from `main`.
- One concern per branch.
- Commit subjects in the imperative: `fix: refuse a start when the nozzle differs`
  rather than `fixed the nozzle check`. The subject is what a `git log` reader
  scans.

## Before opening a pull request

- [ ] Tests pass locally, and the count is 31 or higher
- [ ] A new safety check has a test that shows the refusal, not only the pass
- [ ] No new machine-local absolute path in any file
- [ ] `README.md` still matches what the code does

## Adding a safety check

A check returns a `Check` and never raises. Collect it into the list that
`check_before_start` returns so the caller sees every failure at once. Put the
measured value in `detail`, because that string is what an agent reads to work
out what to fix.
