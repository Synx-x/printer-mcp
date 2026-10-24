# Security

## Reporting a vulnerability

Do not open a public issue for a vulnerability. Report it privately through
GitHub's private advisory form on this repository, under the Security tab.

Include the version, the steps to reproduce, and what an attacker gains. Expect
an acknowledgement within a few days.

## Scope

In scope:

- A safety check that passes when it should refuse. This server starts physical
  hardware, so a check that can be talked past is the most serious defect here.
- Command injection through a profile value, a filename, or a model path.
- An upload or a delete that reaches a path outside the printer's gcode root.

Out of scope:

- Anyone on the network operating the printer. The server has no
  authentication, by design, and both Moonraker and the stock Creality firmware
  are unauthenticated too.
- A profile that sets its own safety limits to something unsafe. The profile is
  trusted input, owned by the person running the server.

## Deploy checklist

Before pointing this at a printer other people can reach:

- [ ] Moonraker is not exposed to the internet, directly or through a port forward
- [ ] The profile lives at a path only the service user can write
- [ ] `max_bed_temp_to_start` and the time budget are set for your machine, not left at the defaults
- [ ] You have tested one refusal, so you know the gate works before you rely on it
- [ ] The printer's own emergency stop is reachable without this server

## Known limitations

- No authentication. Run it on a trusted network.
- Moonraker and Klipper only. A Marlin printer needs a different transport.
- `measure_model` reads STL only. A 3MF is measured by no tool here.
- A profile whose paths do not exist fails at first use, not at load.
