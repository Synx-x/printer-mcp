"""Moonraker (Klipper) REST client.

Deliberately narrow: only the endpoints this MCP needs, so the surface that can
mutate printer state stays small and auditable.

Every call takes an explicit host so the client holds no global state. The
profile owns addressing.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


class PrinterUnreachable(Exception):
    """TCP or HTTP failure reaching the printer."""


class MoonrakerError(Exception):
    """The printer answered, but with an error."""


@dataclass
class MoonrakerClient:
    host: str
    port: int = 7125
    timeout_s: float = 10.0

    # ---- low level ----------------------------------------------------

    @property
    def base(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> Any:
        url = f"{self.base}{path}"
        req = urllib.request.Request(
            url, data=data, method=method, headers=headers or {}
        )
        try:
            with urllib.request.urlopen(
                req, timeout=timeout_s or self.timeout_s
            ) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            raise MoonrakerError(
                f"{method} {path} -> HTTP {exc.code}: {detail}"
            ) from exc
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            raise PrinterUnreachable(
                f"cannot reach printer at {self.host}:{self.port} ({exc}). "
                "Check the printer is powered and on the same subnet as this host."
            ) from exc

        if not body:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return body.decode("utf-8", errors="replace")

    # ---- queries ------------------------------------------------------

    def info(self) -> dict:
        return (self._request("GET", "/printer/info") or {}).get("result", {})

    def query(self, *objects: str) -> dict:
        """Query printer objects, e.g. query('print_stats', 'extruder')."""
        if not objects:
            raise ValueError("query() needs at least one object name")
        joined = "&".join(urllib.parse.quote(o, safe="") for o in objects)
        path = f"/printer/objects/query?{joined}"
        return (self._request("GET", path) or {}).get("result", {}).get("status", {})

    def server_info(self) -> dict:
        return (self._request("GET", "/server/info") or {}).get("result", {})

    def list_files(self, root: str = "gcodes") -> list[dict]:
        res = self._request(
            "GET", f"/server/files/list?root={urllib.parse.quote(root)}"
        )
        return (res or {}).get("result", [])

    # ---- mutations ----------------------------------------------------

    def upload(self, local_path: str, remote_name: str, *, root: str = "gcodes") -> dict:
        """POST a file to Moonraker as multipart/form-data."""
        path = "/server/files/upload"
        boundary = "----printer-mcp-boundary"
        with open(local_path, "rb") as fh:
            payload = fh.read()

        body = b"".join(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="root"\r\n\r\n{root}\r\n'.encode(),
                f"--{boundary}\r\n".encode(),
                (
                    'Content-Disposition: form-data; name="file"; '
                    f'filename="{remote_name}"\r\n'
                ).encode(),
                b"Content-Type: application/octet-stream\r\n\r\n",
                payload,
                f"\r\n--{boundary}--\r\n".encode(),
            ]
        )
        res = self._request(
            "POST",
            path,
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            timeout_s=180.0,
        )
        return res or {}

    def start_print(self, filename: str) -> Any:
        q = urllib.parse.quote(filename)
        return self._request("POST", f"/printer/print/start?filename={q}")

    def pause(self) -> Any:
        return self._request("POST", "/printer/print/pause")

    def resume(self) -> Any:
        return self._request("POST", "/printer/print/resume")

    def cancel(self) -> Any:
        return self._request("POST", "/printer/print/cancel")

    def delete_file(self, filename: str, *, root: str = "gcodes") -> Any:
        q = urllib.parse.quote(f"{root}/{filename}")
        return self._request("DELETE", f"/server/files/{q}")

    def gcode(self, script: str) -> Any:
        """Run a raw gcode script. Gated hard at the tool layer, not here."""
        q = urllib.parse.urlencode({"script": script})
        return self._request("POST", f"/printer/gcode/script?{q}")


def snapshot_url(host: str, port: int | None, path: str) -> str | None:
    """Build the camera URL, or None when the profile disables the camera."""
    if not port:
        return None
    return f"http://{host}:{port}{path}"
