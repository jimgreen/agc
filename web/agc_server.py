"""秦岭微电网 AGC 实时算法后台与 Web HMI 服务。

提供完整 REST API 与静态前端服务（目录 ``web/hmi``），复用 ``AGCRealtimeService``
与 ``AGCRealtimeStore``，支持高并发线程处理与排他端口监听。
"""

from __future__ import annotations

import argparse
import gzip
import json
import mimetypes
import os
import socket
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agc_datasources import (
    DEFAULT_AGC_DB,
    DEFAULT_MODEL_DB,
    DEFAULT_SCADA_DB,
    DEFAULT_SVG_PATH,
)
from agc_service import AGCRealtimeService, STRATEGY_PRESETS

HMI_DIR = Path(__file__).resolve().parent / "hmi"


class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    """Windows 下启用排他端口绑定，避免多实例静默重叠监听。"""

    daemon_threads = True
    allow_reuse_address = False

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        super().server_bind()


def _parse_int(qs: Dict[str, list[str]], key: str, default: int) -> int:
    raw = (qs.get(key) or [""])[0].strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _parse_bool(qs: Dict[str, list[str]], key: str, default: bool = False) -> bool:
    raw = (qs.get(key) or [""])[0].strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def make_handler(service: AGCRealtimeService, static_dir: Path = HMI_DIR):
    resolved_static = static_dir.resolve()

    class AGCHttpHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _send_bytes(
            self,
            status: int,
            body: bytes,
            content_type: str,
            *,
            cache_control: str = "no-store",
            etag: Optional[str] = None,
        ) -> None:
            accept_enc = (self.headers.get("Accept-Encoding") or "").lower()
            use_gzip = len(body) >= 512 and "gzip" in accept_enc
            out = gzip.compress(body, compresslevel=5) if use_gzip else body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            if use_gzip:
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            if etag:
                self.send_header("ETag", etag)
            self.send_header("Content-Length", str(len(out)))
            self.send_header("Cache-Control", cache_control)
            self.end_headers()
            self.wfile.write(out)

        def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
            raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self._send_bytes(status, raw, "application/json; charset=utf-8")

        def _read_json_body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            if length > 5_000_000:
                raise ValueError("请求体超过 5MB 上限")
            raw = self.rfile.read(length)
            if not raw:
                return {}
            parsed = json.loads(raw.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("JSON 请求体必须为对象")
            return parsed

        def _serve_static(self, rel_path: str) -> None:
            clean = rel_path.lstrip("/") or "index.html"
            candidate = (resolved_static / clean).resolve()
            if not str(candidate).startswith(str(resolved_static)) or not candidate.is_file():
                self._send_json({"error": "Not Found", "path": rel_path}, HTTPStatus.NOT_FOUND)
                return
            ctype, _ = mimetypes.guess_type(str(candidate))
            if candidate.suffix.lower() == ".js":
                ctype = "application/javascript; charset=utf-8"
            elif candidate.suffix.lower() == ".css":
                ctype = "text/css; charset=utf-8"
            elif candidate.suffix.lower() == ".html":
                ctype = "text/html; charset=utf-8"
            elif candidate.suffix.lower() == ".svg":
                ctype = "image/svg+xml; charset=utf-8"
            else:
                ctype = ctype or "application/octet-stream"
            st = candidate.stat()
            etag = f'W/"{st.st_mtime_ns:x}-{st.st_size:x}"'
            inm = (self.headers.get("If-None-Match") or "").strip()
            if inm and inm == etag:
                self.send_response(HTTPStatus.NOT_MODIFIED)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                return
            self._send_bytes(HTTPStatus.OK, candidate.read_bytes(), ctype, cache_control="no-cache", etag=etag)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query, keep_blank_values=True)
            try:
                if path == "/api/health":
                    self._send_json(
                        {
                            "ok": True,
                            "service": "qinling-agc",
                            "pid": os.getpid(),
                            "started_at": service.started_at,
                            "model_db": str(service.adapter.model_db),
                            "scada_db": str(service.adapter.scada_db),
                            "svg_file": str(service.adapter.svg_file or ""),
                            "agc_db": str(service.store.path),
                            "auto_enabled": service.auto_enabled,
                            "loop_mode": service.loop_mode,
                        }
                    )
                    return

                if path == "/api/snapshot":
                    include_static = _parse_bool(qs, "static", False)
                    lite = _parse_bool(qs, "lite", False)
                    raw_since = (qs.get("since_cycle") or [""])[0].strip()
                    raw_rev = (qs.get("since_rev") or [""])[0].strip()
                    since_cycle = int(raw_since) if raw_since.lstrip("-").isdigit() else None
                    since_rev = int(raw_rev) if raw_rev.lstrip("-").isdigit() else None
                    self._send_json(
                        service.get_snapshot_payload(
                            include_static=include_static,
                            lite=lite,
                            since_cycle=since_cycle,
                            since_rev=since_rev,
                        )
                    )
                    return

                if path == "/api/diagram/svg":
                    svg = service.adapter.svg_text
                    if not svg:
                        self._send_json({"error": "未找到可用的 SVG 接线图文件"}, HTTPStatus.NOT_FOUND)
                        return
                    self._send_bytes(
                        HTTPStatus.OK,
                        svg.encode("utf-8"),
                        "image/svg+xml; charset=utf-8",
                        cache_control="no-cache",
                    )
                    return

                if path == "/api/model":
                    snap = service.get_snapshot_payload(include_static=True)
                    self._send_json(
                        {
                            "model": snap.get("model", {}),
                            "devices": snap.get("devices", []),
                            "device_parameters": snap.get("device_parameters", {}),
                        }
                    )
                    return

                if path == "/api/measurements/trace":
                    dev_type = (qs.get("dev_type") or [""])[0]
                    dev_name = (qs.get("dev_name") or [""])[0]
                    meas_type = (qs.get("meas_type") or [""])[0]
                    window = _parse_int(qs, "window", 60)
                    pts = service.get_measurement_trace(dev_type, dev_name, meas_type, window_minutes=window)
                    self._send_json(
                        {
                            "dev_type": dev_type,
                            "dev_name": dev_name,
                            "meas_type": meas_type,
                            "window_minutes": window,
                            "points": pts,
                        }
                    )
                    return

                if path == "/api/commands":
                    only_active = _parse_bool(qs, "only_active", False)
                    origin = (qs.get("origin") or ["all"])[0] or "all"
                    self._send_json(service.get_commands_payload(only_active=only_active, origin=origin))
                    return

                if path == "/api/commands/trace":
                    dev_type = (qs.get("dev_type") or [""])[0]
                    dev_name = (qs.get("dev_name") or [""])[0]
                    set_type = (qs.get("set_type") or [""])[0]
                    window = _parse_int(qs, "window", 60)
                    pts = service.store.query_command_trace(
                        dev_type=dev_type,
                        dev_name=dev_name,
                        set_type=set_type,
                        window_minutes=window,
                    )
                    self._send_json({"points": pts, "window_minutes": window})
                    return

                if path == "/api/agc/control":
                    self._send_json(service.get_agc_control_payload())
                    return

                if path == "/api/agc/strategy":
                    raw_cycle = (qs.get("cycle") or [""])[0].strip()
                    cycle_no = int(float(raw_cycle)) if raw_cycle else None
                    res = service.get_strategy_cycle_payload(cycle_no)
                    self._send_json(res, HTTPStatus.OK if res.get("ok") else HTTPStatus.NOT_FOUND)
                    return

                if path == "/api/agc/device-tracking":
                    dev_type = (qs.get("dev_type") or [""])[0]
                    dev_name = (qs.get("dev_name") or [""])[0]
                    set_type = (qs.get("set_type") or ["p_set"])[0] or "p_set"
                    window = _parse_int(qs, "window", 60)
                    limit = _parse_int(qs, "limit", 600)
                    self._send_json(
                        service.get_device_tracking_payload(
                            dev_type=dev_type,
                            dev_name=dev_name,
                            set_type=set_type,
                            window_minutes=window,
                            limit=limit,
                        )
                    )
                    return

                if path == "/api/device-configs/export":
                    self._send_json(service.export_device_config_bundle())
                    return

                if path == "/api/agc/presets":
                    self._send_json({"presets": STRATEGY_PRESETS})
                    return

                if path == "/api/agc/trend":
                    window = _parse_int(qs, "window", 60)
                    limit = _parse_int(qs, "limit", 600)
                    raw_end = (qs.get("end_epoch_ms") or [""])[0].strip()
                    end_ms = int(float(raw_end)) if raw_end else None
                    self._send_json(
                        service.store.query_trend(
                            window_minutes=window,
                            end_epoch_ms=end_ms,
                            limit=limit,
                        )
                    )
                    return

                if path == "/api/agc/logs":
                    page = _parse_int(qs, "page", 1)
                    page_size = _parse_int(qs, "page_size", 20)
                    level = (qs.get("level") or ["all"])[0]
                    keyword = (qs.get("keyword") or [""])[0]
                    self._send_json(
                        service.store.query_control_logs(
                            page=page,
                            page_size=page_size,
                            level=level,
                            keyword=keyword,
                        )
                    )
                    return

                if path == "/api/runtime-settings":
                    self._send_json(service.store.load_runtime_settings())
                    return

                if path == "/api/device-configs":
                    self._send_json(service.get_device_configs_payload())
                    return

                if path == "/api/device-configs/curve":
                    dev_type = (qs.get("dev_type") or [""])[0]
                    dev_name = (qs.get("dev_name") or [""])[0]
                    set_type = (qs.get("set_type") or ["p_set"])[0] or "p_set"
                    self._send_json(
                        service.get_device_curve_payload(
                            dev_type=dev_type,
                            dev_name=dev_name,
                            set_type=set_type,
                        )
                    )
                    return

                if path == "/api/runtime-logs":
                    page = _parse_int(qs, "page", 1)
                    page_size = _parse_int(qs, "page_size", 20)
                    log_type = (qs.get("log_type") or ["all"])[0]
                    only_alert = _parse_bool(qs, "only_alert", False)
                    keyword = (qs.get("keyword") or [""])[0]
                    self._send_json(
                        service.store.query_runtime_logs(
                            page=page,
                            page_size=page_size,
                            log_type=log_type,
                            only_alert=only_alert,
                            keyword=keyword,
                        )
                    )
                    return

                self._serve_static(path)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                body = self._read_json_body()

                if path == "/api/agc/control":
                    action = str(body.get("action") or "").strip().lower()
                    if action == "calculate_once":
                        self._send_json({"ok": True, "control": service.run_control_cycle(trigger="manual")})
                        return
                    if action == "start":
                        self._send_json({"ok": True, "control": service.set_auto_enabled(True)})
                        return
                    if action == "stop":
                        self._send_json({"ok": True, "control": service.set_auto_enabled(False)})
                        return
                    if action == "set_mode":
                        mode = str(body.get("loop_mode") or "open")
                        self._send_json({"ok": True, "control": service.set_loop_mode(mode)})
                        return
                    if action == "save_settings":
                        params = body.get("settings") or {}
                        preset = str(body.get("preset") or "custom")
                        self._send_json(
                            {"ok": True, "control": service.update_control_parameters(params, preset=preset)}
                        )
                        return
                    if action == "apply_preset":
                        preset = str(body.get("preset") or "standard")
                        self._send_json(
                            {"ok": True, "control": service.update_control_parameters({}, preset=preset)}
                        )
                        return
                    if action == "reload_model":
                        service.adapter.reload_static()
                        service.refresh_telemetry()
                        self._send_json({"ok": True, "control": service.get_agc_control_payload()})
                        return
                    self._send_json({"ok": False, "error": f"不支持的控制动作: {action}"}, HTTPStatus.BAD_REQUEST)
                    return

                if path == "/api/agc/preview":
                    res = service.preview_control_cycle(
                        override_settings=body.get("settings"),
                        preset=body.get("preset"),
                        override_device_configs=body.get("device_configs"),
                    )
                    self._send_json(res)
                    return

                if path == "/api/commands/manual":
                    res = service.execute_manual_command(
                        dev_type=str(body.get("dev_type") or ""),
                        dev_name=str(body.get("dev_name") or ""),
                        set_type=str(body.get("set_type") or ""),
                        value=body.get("value"),
                    )
                    self._send_json(res, HTTPStatus.OK if res.get("ok") else HTTPStatus.BAD_GATEWAY)
                    return

                if path == "/api/runtime-settings":
                    saved = service.update_runtime_settings(
                        backend=body.get("backend"),
                        web=body.get("web"),
                        reset_scope=str(body.get("reset_scope") or ""),
                    )
                    self._send_json({"ok": True, "runtime_settings": saved})
                    return

                if path == "/api/device-configs":
                    items = body.get("items")
                    if items is None and body.get("dev_type") and body.get("dev_name"):
                        items = [body]
                    res = service.save_device_configs(items or [])
                    self._send_json({"ok": True, **res})
                    return

                if path == "/api/device-configs/reset":
                    res = service.reset_device_configs(
                        dev_type=str(body.get("dev_type") or ""),
                        dev_name=str(body.get("dev_name") or ""),
                        set_type=str(body.get("set_type") or ""),
                        reset_curves=bool(body.get("reset_curves", False)),
                    )
                    self._send_json({"ok": True, **res})
                    return

                if path == "/api/device-configs/curve":
                    res = service.save_device_curve(
                        dev_type=str(body.get("dev_type") or ""),
                        dev_name=str(body.get("dev_name") or ""),
                        set_type=str(body.get("set_type") or "p_set"),
                        points=body.get("points") or [],
                        curve_interp=body.get("curve_interp"),
                    )
                    self._send_json({"ok": True, **res})
                    return

                if path == "/api/device-configs/curve/copy":
                    res = service.copy_device_curve_to_targets(
                        source_name=str(body.get("source_name") or ""),
                        points=body.get("points") or [],
                        curve_interp=body.get("curve_interp"),
                        targets=body.get("targets") or [],
                    )
                    self._send_json({"ok": True, **res})
                    return

                if path == "/api/device-configs/import":
                    res = service.import_device_config_bundle(body)
                    self._send_json({"ok": True, **res})
                    return

                if path == "/api/agc/emergency-stop":
                    reason = str(body.get("reason") or "操作员触发紧急停控")
                    self._send_json({"ok": True, "control": service.emergency_stop(reason=reason)})
                    return

                if path == "/api/agc/prune-history":
                    res = service.prune_history_now(reason="manual")
                    self._send_json(res)
                    return

                if path == "/api/runtime-logs/clear":
                    deleted = service.store.clear_runtime_logs()
                    self._send_json({"ok": True, "deleted": deleted})
                    return

                if path == "/api/agc/logs/clear":
                    deleted = service.store.clear_control_logs()
                    self._send_json({"ok": True, "deleted": deleted})
                    return

                self._send_json({"ok": False, "error": f"未知接口: {path}"}, HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    return AGCHttpHandler


def create_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    model_db: str | Path = DEFAULT_MODEL_DB,
    scada_db: str | Path = DEFAULT_SCADA_DB,
    svg_path: str | Path = DEFAULT_SVG_PATH,
    agc_db: str | Path = DEFAULT_AGC_DB,
    command_endpoint: Optional[str | Path] = None,
    command_sender=None,
    start_background: bool = True,
) -> tuple[ExclusiveThreadingHTTPServer, AGCRealtimeService]:
    """创建排他监听的 AGC HTTP 服务与后台算法服务实例。"""
    service = AGCRealtimeService(
        model_db=model_db,
        scada_db=scada_db,
        svg_path=svg_path,
        agc_db=agc_db,
        command_endpoint=command_endpoint,
        command_sender=command_sender,
        start_background=start_background,
    )
    handler_cls = make_handler(service, HMI_DIR)
    try:
        server = ExclusiveThreadingHTTPServer((host, int(port)), handler_cls)
    except BaseException:
        service.close()
        raise
    return server, service


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="秦岭微电网 AGC 算法后台与 Web HMI 服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8765, help="监听端口")
    parser.add_argument("--model-db", default=str(DEFAULT_MODEL_DB), help="输入模型库 (devices.db)")
    parser.add_argument("--scada-db", default=str(DEFAULT_SCADA_DB), help="实时数据库 (scdfe_d.db)")
    parser.add_argument("--svg", default=str(DEFAULT_SVG_PATH), help="SVG 接线图或关联 .e 路径")
    parser.add_argument("--agc-db", default=str(DEFAULT_AGC_DB), help="AGC 持久化库 (agc.db)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    server, service = create_server(
        host=args.host,
        port=args.port,
        model_db=args.model_db,
        scada_db=args.scada_db,
        svg_path=args.svg,
        agc_db=args.agc_db,
        start_background=True,
    )
    print(
        f"[AGC Web] 服务已启动: http://{args.host}:{args.port}/\n"
        f"  模型库: {service.adapter.model_db}\n"
        f"  实时库: {service.adapter.scada_db}\n"
        f"  接线图: {service.adapter.svg_file}\n"
        f"  AGC 库: {service.store.path}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
