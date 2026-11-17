"""Small, dependency-free bridge between AGC and the SCADA four-remote API.

Deployments can provide a Python ``fes_interface`` module, configure an HTTP
SCADA API with ``AGC_SCADA_API_URL``, or configure the authenticated scdfe
command endpoint with ``AGC_SCADA_COMMAND_ENDPOINT``.  The bridge deliberately
does not invent success when no command endpoint is configured: reads fall
back to the model for simulation, while writes return ``False`` and expose a
diagnostic reason to the AGC logger.
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def _env(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default) or "").strip()


def result_ok(value: Any) -> bool:
    """Interpret common SCADA command response shapes conservatively."""
    if isinstance(value, tuple):
        return result_ok(value[0]) if value else False
    if isinstance(value, list):
        return result_ok(value[0]) if value else False
    if isinstance(value, dict):
        marker = next((value[name] for name in ("ok", "success", "accepted") if name in value), None)
        if marker is not None:
            return result_ok(marker)
        if "status" in value:
            return result_ok(value["status"])
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"ok", "success", "accepted", "true", "1", "done"}:
            return True
        if normalized in {"", "error", "failed", "failure", "false", "0", "rejected", "denied"}:
            return False
    # A command API that returns None after successful submission is common;
    # the caller still records it as submitted when no exception was raised.
    return value is None or bool(value)


_result_ok = result_ok


def normalize_read_result(value: Any) -> tuple[bool, str, Any]:
    """Normalize legacy FES/HTTP responses to ``(ok, name, value)``."""
    if isinstance(value, (tuple, list)):
        if len(value) >= 3:
            return bool(value[0]), str(value[1] or ""), value[2]
        if len(value) == 2:
            if isinstance(value[0], bool):
                return value[0], "", value[1]
            return True, str(value[0] or ""), value[1]
        if len(value) == 1:
            return normalize_read_result(value[0])
        return False, "", None
    if isinstance(value, dict):
        ok = result_ok(value)
        name = str(value.get("name", value.get("description", "")) or "")
        payload = value.get("value")
        if payload is None:
            payload = value.get("data", value.get("result"))
            if isinstance(payload, dict):
                name = str(payload.get("name", name) or "")
                payload = payload.get("value", payload.get("data"))
        return ok, name, payload
    if isinstance(value, bool):
        return value, "", None
    if value is None:
        return False, "", None
    return True, "", value


class ScadaHttpClient:
    """Adapter for the scdfe REST point API.

    Expected read route: ``GET /api/points/{kind}?rtu_id=&pnt_no=``.
    Expected write route: ``POST /api/points/{kind}/{rtu_id}/{pnt_no}`` with
    ``{"value": ...}``; deployments with a different route can set
    ``AGC_SCADA_COMMAND_URL`` to a URL containing ``{kind}``, ``{rtu}`` and
    ``{pnt}`` placeholders.
    """

    def __init__(self, base_url: str, timeout: float = 3.0, token: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = max(0.1, float(timeout))
        self.token = token
        self.command_url = _env("AGC_SCADA_COMMAND_URL")

    def _request(self, method: str, url: str, payload: Optional[dict] = None) -> Any:
        headers = {"Accept": "application/json"}
        data = None
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        with urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _rows(payload: Any) -> list[dict]:
        if isinstance(payload, dict):
            rows = payload.get("items", payload.get("rows", payload.get("data", payload)))
        else:
            rows = payload
        if isinstance(rows, dict):
            rows = [rows]
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def read(self, kind: str, rtu: Any, pnt: Any) -> tuple[bool, str, Any]:
        query = urlencode({"rtu_id": int(rtu), "pnt_no": int(pnt), "limit": 1})
        payload = self._request("GET", f"{self.base_url}/api/points/{kind}?{query}")
        rows = self._rows(payload)
        if not rows:
            return False, "", None
        row = rows[0]
        return True, str(row.get("name", row.get("description", ""))), row.get("value")

    def send(self, kind: str, rtu: Any, pnt: Any, value: Any) -> bool:
        if self.command_url:
            url = self.command_url.format(kind=kind, rtu=int(rtu), pnt=int(pnt))
        else:
            url = f"{self.base_url}/api/points/{kind}/{int(rtu)}/{int(pnt)}"
        payload = self._request("POST", url, {"value": value})
        return result_ok(payload)


class ScadaCommandClient:
    """Authenticated client for scdfe_c's newline-delimited command service."""

    def __init__(self, endpoint_file: str | Path, timeout: float = 5.0) -> None:
        self.endpoint_file = Path(endpoint_file)
        self.timeout = max(0.1, float(timeout))

    def send(self, kind: str, rtu: Any, pnt: Any, value: Any) -> bool:
        record = json.loads(self.endpoint_file.read_text(encoding="utf-8"))
        host = record.get("host")
        port = record.get("port")
        token = record.get("token")
        if not isinstance(host, str) or not isinstance(port, int) or not isinstance(token, str):
            raise RuntimeError("SCADA 指令端点文件无效")
        payload = {
            "request_id": uuid.uuid4().hex,
            "op": "command",
            "rtu_id": int(rtu),
            "kind": str(kind),
            "point_no": int(pnt),
            "value": int(bool(value)) if kind == "yk" else float(value),
            "token": token,
        }
        raw = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with socket.create_connection((host, port), timeout=self.timeout) as connection:
            connection.settimeout(self.timeout)
            connection.sendall(raw)
            response = json.loads(connection.makefile("rb").readline(65537).decode("utf-8"))
        if not response.get("ok"):
            raise RuntimeError(str(response.get("error") or "SCADA 拒绝指令"))
        return True


def command_endpoint_from_env() -> Optional[Path]:
    configured = _env("AGC_SCADA_COMMAND_ENDPOINT")
    if configured:
        return Path(configured)
    database = _env("AGC_SCADA_DB")
    if database:
        candidate = Path(database).with_name(Path(database).name + ".scdfe_command.json")
        if candidate.exists():
            return candidate
    return None


def load_http_client() -> Optional[ScadaHttpClient]:
    url = _env("AGC_SCADA_API_URL")
    if not url:
        return None
    return ScadaHttpClient(url, _env("AGC_SCADA_TIMEOUT", "3"), _env("AGC_SCADA_TOKEN"))


def resolve_callable(module: Any, names: tuple[str, ...]) -> Optional[Callable]:
    for name in names:
        candidate = getattr(module, name, None)
        if callable(candidate):
            return candidate
    return None
