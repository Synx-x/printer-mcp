# Contributing

The most confusing thing about this repo is that nothing you read names a
printer. That is deliberate. `printer_mcp` is generic, and every machine fact
arrives through one JSON profile. If you find yourself wanting to add a default
temperature or a default path to a module, the change belongs in the profile
schema instead.

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
env -u PYTEST_ADDOPTS .venv/bin/python -m pytest tests/ -q
```

Expect `31 passed`. The suite needs no printer and no network. `env -u
PYTEST_ADDOPTS` matters because a global pytest config can inject a plugin this
repo does not install, and the run then dies on import.

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
