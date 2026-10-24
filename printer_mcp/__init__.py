"""printer-mcp: an MCP server for driving a 3D printer, configured by JSON profile."""

from .config import (
    Material,
    PrinterEndpoint,
    Profile,
    ProfileError,
    SafetyLimits,
    SlicerConfig,
    load_profile,
)
from .moonraker import MoonrakerClient, MoonrakerError, PrinterUnreachable
from .slicer import SliceFailed, measure_stl, slice_model

__version__ = "0.1.0"

__all__ = [
    "Material",
    "MoonrakerClient",
    "MoonrakerError",
    "PrinterEndpoint",
    "PrinterUnreachable",
    "Profile",
    "ProfileError",
    "SafetyLimits",
    "SliceFailed",
    "SlicerConfig",
    "__version__",
    "load_profile",
    "measure_stl",
    "slice_model",
]


def __getattr__(name: str):
    """Import the server lazily so `python -m printer_mcp.server` stays clean."""
    if name in ("build_server", "main"):
        from . import server as _server

        return getattr(_server, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
