"""Tests for profile loading and the safety gates.

These run without a printer. Every check is exercised against synthetic gcode so
the suite is meaningful in CI and on a machine with no hardware attached.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from printer_mcp.config import ProfileError, load_profile
from printer_mcp.safety import (
    bed_fit_check,
    check_before_start,
    check_filename,
    parse_duration,
    read_gcode_facts,
)


# ---- fixtures --------------------------------------------------------


def write_profile(tmp_path: Path, **overrides) -> Path:
    """Minimal valid profile, with fields overrideable per test."""
    data = {
        "name": "test printer",
        "printer": {"host": "127.0.0.1"},
        "machine": {"nozzle_diameter": 0.4, "build_volume": [220, 220, 245]},
        "slicer": {"binary": "/bin/true"},
        "materials": {
            "pla": {
                "bed_temp": 60,
                "nozzle_temp": 215,
                "filament_profiles": ["/tmp/pla.json"],
            }
        },
        "process_profiles": {"std": "/tmp/std.json"},
        "safety": {
            "max_print_time_s": 7200,
            "max_filament_cm3": 100.0,
            "max_bed_temp_to_start": 45.0,
        },
    }
    data.update(overrides)
    p = tmp_path / "profile.json"
    p.write_text(json.dumps(data))
    return p


def write_gcode(
    tmp_path: Path,
    *,
    time_text: str = "1h 2m 31s",
    cm3: float = 21.37,
    nozzle: float = 0.4,
    complete: bool = True,
    include_meta: bool = True,
) -> Path:
    """Synthetic gcode. Metadata goes after the executable block, as Orca writes it."""
    body = ["; HEADER_BLOCK_START", "M140 S70", "G28", "M104 S250", "G1 X10 Y10 E1"]
    if complete:
        body.append("; EXECUTABLE_BLOCK_END")
    if include_meta:
        body += [
            f"; filament used [cm3] = {cm3}",
            f"; estimated printing time (normal mode) = {time_text}",
            f"; nozzle_diameter = {nozzle}",
            "; layer_height = 0.2",
            "; CONFIG_BLOCK_END",
        ]
    else:
        body += ["; CONFIG_BLOCK_END"]
    p = tmp_path / "part.gcode"
    p.write_text("\n".join(body) + "\n")
    return p


@pytest.fixture
def profile(tmp_path):
    return load_profile(write_profile(tmp_path))


@pytest.fixture
def idle_cool_state():
    return {"state": "standby", "bed_temp": 24.0, "nozzle_diameter": 0.4}


# ---- duration parsing ------------------------------------------------


@pytest.mark.parametrize(
    "text,seconds",
    [
        ("1h 2m 31s", 3751),
        ("32m 24s", 1944),
        ("45s", 45),
        ("2h", 7200),
        ("3h 5m 38s", 11138),
    ],
)
def test_parse_duration(text, seconds):
    assert parse_duration(text) == seconds


def test_parse_duration_rejects_garbage():
    assert parse_duration("soon") is None


# ---- gcode facts -----------------------------------------------------


def test_reads_metadata_written_after_the_executable_block(tmp_path):
    facts = read_gcode_facts(write_gcode(tmp_path))
    assert facts.estimated_seconds == 3751
    assert facts.filament_cm3 == pytest.approx(21.37)
    assert facts.nozzle_diameter == pytest.approx(0.4)
    assert facts.complete is True
    assert facts.problems == []


def test_truncated_gcode_is_flagged(tmp_path):
    p = write_gcode(tmp_path, complete=False, include_meta=False)
    facts = read_gcode_facts(p)
    assert facts.complete is False
    assert any("truncated" in prob for prob in facts.problems)


def test_missing_file_is_reported_not_raised(tmp_path):
    facts = read_gcode_facts(tmp_path / "nope.gcode")
    assert facts.complete is False
    assert any("not found" in prob for prob in facts.problems)


# ---- the happy path --------------------------------------------------


def test_all_checks_pass_on_a_good_job(tmp_path, profile, idle_cool_state):
    facts = read_gcode_facts(write_gcode(tmp_path))
    checks = check_before_start(
        facts=facts,
        profile=profile,
        live_state=idle_cool_state,
        live_nozzle=0.4,
    )
    failures = [c for c in checks if not c.ok]
    assert failures == [], [c.detail for c in failures]


# ---- each refusal, individually -------------------------------------


def test_refuses_nozzle_mismatch(tmp_path, profile, idle_cool_state):
    facts = read_gcode_facts(write_gcode(tmp_path, nozzle=0.2))
    checks = {c.name: c for c in check_before_start(
        facts=facts, profile=profile, live_state=idle_cool_state, live_nozzle=0.4
    )}
    assert checks["nozzle_matches_profile"].ok is False


def test_refuses_over_time_budget(tmp_path, profile, idle_cool_state):
    facts = read_gcode_facts(write_gcode(tmp_path, time_text="4h 0m 0s"))
    checks = {c.name: c for c in check_before_start(
        facts=facts, profile=profile, live_state=idle_cool_state, live_nozzle=0.4
    )}
    assert checks["print_time_within_limit"].ok is False


def test_refuses_over_filament_budget(tmp_path, profile, idle_cool_state):
    facts = read_gcode_facts(write_gcode(tmp_path, cm3=250.0))
    checks = {c.name: c for c in check_before_start(
        facts=facts, profile=profile, live_state=idle_cool_state, live_nozzle=0.4
    )}
    assert checks["filament_within_limit"].ok is False


def test_refuses_when_printer_is_printing(tmp_path, profile):
    facts = read_gcode_facts(write_gcode(tmp_path))
    busy = {"state": "printing", "bed_temp": 60.0, "nozzle_diameter": 0.4}
    checks = {c.name: c for c in check_before_start(
        facts=facts, profile=profile, live_state=busy, live_nozzle=0.4
    )}
    assert checks["printer_idle"].ok is False


def test_refuses_when_bed_is_hot(tmp_path, profile):
    facts = read_gcode_facts(write_gcode(tmp_path))
    hot = {"state": "standby", "bed_temp": 69.0, "nozzle_diameter": 0.4}
    checks = {c.name: c for c in check_before_start(
        facts=facts, profile=profile, live_state=hot, live_nozzle=0.4
    )}
    assert checks["bed_cool"].ok is False


def test_refuses_live_nozzle_mismatch(tmp_path, profile, idle_cool_state):
    facts = read_gcode_facts(write_gcode(tmp_path))
    checks = {c.name: c for c in check_before_start(
        facts=facts, profile=profile, live_state=idle_cool_state, live_nozzle=0.6
    )}
    assert checks["nozzle_matches_live_machine"].ok is False


def test_refuses_truncated_gcode(tmp_path, profile, idle_cool_state):
    facts = read_gcode_facts(
        write_gcode(tmp_path, complete=False, include_meta=False)
    )
    checks = {c.name: c for c in check_before_start(
        facts=facts, profile=profile, live_state=idle_cool_state, live_nozzle=0.4
    )}
    assert checks["gcode_complete"].ok is False
    assert checks["print_time_within_limit"].ok is False


# ---- filenames -------------------------------------------------------


@pytest.mark.parametrize(
    "name,ok",
    [
        ("fence-petg-v2.gcode", True),
        ("part_1.gcode", True),
        ("my part.gcode", False),
        ("garbage.gcode", True),
        ("pärt.gcode", False),
        ("a/b.gcode", False),
    ],
)
def test_filename_safety(name, ok):
    assert check_filename(name, enforce=True).ok is ok


# ---- build volume ----------------------------------------------------


def test_bed_fit_accepts_and_rejects():
    assert bed_fit_check(
        dimensions=(100, 50, 20), build_volume=(220, 220, 245)
    ).ok is True
    too_big = bed_fit_check(
        dimensions=(240, 50, 20), build_volume=(220, 220, 245)
    )
    assert too_big.ok is False
    assert "240" in too_big.detail


# ---- profile validation ---------------------------------------------


def test_missing_required_key_is_a_profile_error(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(
        json.dumps(
            {
                "name": "x",
                "printer": {"host": "h"},
                "slicer": {"binary": "/bin/true"},
            }
        )
    )
    with pytest.raises(ProfileError, match="machine"):
        load_profile(p)


def test_bad_json_is_a_profile_error(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    with pytest.raises(ProfileError, match="invalid JSON"):
        load_profile(p)


def test_no_profile_found_raises_with_guidance(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PRINTER_MCP_PROFILE", raising=False)
    with pytest.raises(ProfileError, match="no printer profile found"):
        load_profile(tmp_path / "absent.json", required=True)


def test_env_var_selects_the_profile(tmp_path, monkeypatch):
    p = write_profile(tmp_path, name="from env")
    monkeypatch.setenv("PRINTER_MCP_PROFILE", str(p))
    assert load_profile().name == "from env"


def test_unknown_material_names_the_known_set(tmp_path):
    prof = load_profile(write_profile(tmp_path))
    with pytest.raises(ProfileError, match="unknown material"):
        prof.material("unobtanium")


def test_build_volume_must_be_three_numbers(tmp_path):
    p = write_profile(
        tmp_path, machine={"nozzle_diameter": 0.4, "build_volume": [220, 220]}
    )
    with pytest.raises(ProfileError, match="build_volume"):
        load_profile(p)


# ---- portable paths ----------------------------------------------


def test_tilde_paths_expand_in_every_path_field(tmp_path, monkeypatch):
    """A published profile uses ~, so the loader must expand it at load time."""
    monkeypatch.setenv("HOME", "/tmp/fakehome")
    p = write_profile(
        tmp_path,
        slicer={
            "binary": "~/orca.AppImage",
            "machine_profile": "~/machine.json",
            "profiles_dir": "~/profiles",
        },
        materials={
            "pla": {
                "bed_temp": 60,
                "nozzle_temp": 215,
                "filament_profiles": ["~/filament.json"],
            }
        },
        process_profiles={"std": "~/std.json"},
    )
    prof = load_profile(p)
    assert prof.slicer.binary == "/tmp/fakehome/orca.AppImage"
    assert prof.slicer.machine_profile == "/tmp/fakehome/machine.json"
    assert prof.slicer.profiles_dir == Path("/tmp/fakehome/profiles")
    assert prof.materials["pla"].filament_profiles == ["/tmp/fakehome/filament.json"]
    assert prof.process_profiles["std"] == "/tmp/fakehome/std.json"
