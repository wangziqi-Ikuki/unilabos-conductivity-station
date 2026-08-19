"""电导工站 TCP 协议模拟服务端，可选本机交互页改库位与设备在线。"""

from __future__ import annotations

import argparse
import copy
import json
import math
import socketserver
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from yb_sse_devices.protocol import (
    DEVICE_KEYS,
    DEVICE_LABELS,
    LAYER_LABELS,
    RACK_LAYERS,
    RESULT_MESSAGES,
    SLOT_COUNT,
    SLOT_LABELS,
    STEP_NAMES,
    demo_loaded_materials,
    empty_materials,
    online_devices,
    parse_slot_index,
    resolve_layer,
)

# 合作方高保真模型确认使用直径 1 cm 圆片：A = π × (0.5 cm)²。
DISC_AREA_CM2 = math.pi * 0.5**2


class MockConductivityState:
    """线程安全的批次模拟状态。"""

    def __init__(
        self,
        *,
        step_interval: float = 0.1,
        failure_sample: int | None = None,
        failure_step: int = 10,
        materials: dict[str, list[int]] | None = None,
        devices: dict[str, int] | None = None,
        auto_advance: bool = True,
    ) -> None:
        self.step_interval = max(0.01, float(step_interval))
        self.failure_sample = failure_sample
        self.failure_step = int(failure_step)
        self.auto_advance = bool(auto_advance)
        self.lock = threading.RLock()
        self.started_monotonic: float | None = None
        self.stop_requested = False
        self.stop_after_sample: int | None = None
        self.batch: dict[str, Any] | None = None
        self.history: dict[str, dict[str, Any]] = {}
        self.materials = copy.deepcopy(
            materials if materials is not None else empty_materials()
        )
        self.devices = dict(devices or online_devices(True))
        self._manual_busy_until = 0.0
        self.last_client = ""
        self.last_action: dict[str, Any] | None = None

    @staticmethod
    def _positions(values: list[int]) -> list[int]:
        return [index for index, occupied in enumerate(values, start=1) if occupied]

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            self._update()
            batch = self.batch
            step = 0 if not batch else int(batch.get("current_test_step") or 0)
            return {
                "devices": dict(self.devices),
                "materials": copy.deepcopy(self.materials),
                "running": bool(batch and batch.get("running")),
                "batch_id": None if not batch else batch.get("batch_id"),
                "batch_state": "" if not batch else str(batch.get("state") or ""),
                "current_test": 0 if not batch else int(batch.get("current_test") or 0),
                "current_test_step": step,
                "current_step_name": STEP_NAMES.get(step, ""),
                "finished_count": 0 if not batch else int(batch.get("finished_count") or 0),
                "failed_count": 0 if not batch else int(batch.get("failed_count") or 0),
                "last_client": self.last_client,
                "last_action": None if not self.last_action else dict(self.last_action),
                "auto_advance": self.auto_advance,
            }

    def set_slot(self, layer: str, index: int, occupied: bool) -> None:
        if layer not in RACK_LAYERS:
            raise ValueError(f"未知料架层: {layer}")
        if not 0 <= index < SLOT_COUNT:
            raise ValueError("槽位下标必须在 0 到 9 之间")
        with self.lock:
            self.materials[layer][index] = 1 if occupied else 0

    def set_device(self, key: str, online: bool) -> None:
        if key not in DEVICE_KEYS:
            raise ValueError(f"未知设备: {key}")
        with self.lock:
            self.devices[key] = 1 if online else 0

    def clear_materials(self) -> None:
        with self.lock:
            self.materials = empty_materials()

    def load_demo_materials(self) -> None:
        with self.lock:
            self.materials = demo_loaded_materials()

    def set_auto_advance(self, enabled: bool) -> None:
        with self.lock:
            self.auto_advance = bool(enabled)

    def reset(self) -> None:
        """清空库位、批次和最近动作，设备全部回到在线。"""
        with self.lock:
            self.materials = empty_materials()
            self.devices = online_devices(True)
            self.batch = None
            self.started_monotonic = None
            self.stop_requested = False
            self.stop_after_sample = None
            self._manual_busy_until = 0.0
            self.last_action = None

    def _plc_offline(self) -> bool:
        return all(int(self.devices.get(key, 0)) == 0 for key in DEVICE_KEYS)

    def _new_test(self, index: int, bottle: int, mold: int, funnel: int) -> dict[str, Any]:
        return {
            "state": "not_started",
            "test_time": "",
            "bottle": bottle,
            "mold": mold,
            "funnel": funnel,
            "step": 0,
            "bottle_code": "",
            "recipe": "",
            "formula": "",
            "temperature": 0.0,
            "due_pressure": 3,
            "pressure_time": 10,
            "R": 0.0,
            "h1": 0.0,
            "h2": 0.0,
            "area": DISC_AREA_CM2,
            "ion_conductivity": 0.0,
            "elec_conductivity_tested": False,
            "elec_R": 0.0,
            "elec_conductivity": 0.0,
            "_sample_index": index,
        }

    def _update(self) -> None:
        if not self.auto_advance:
            return
        if not self.batch or self.started_monotonic is None or not self.batch["running"]:
            return
        tests = self.batch["tests"]
        elapsed_steps = int((time.monotonic() - self.started_monotonic) / self.step_interval)
        target_completed = elapsed_steps // 13
        active_step = elapsed_steps % 13 + 1

        if self.stop_after_sample is not None:
            target_completed = min(target_completed, self.stop_after_sample)

        for index, test in enumerate(tests, start=1):
            if test["state"] in {"finished", "failed"}:
                continue
            failure_reached = (
                index == self.failure_sample
                and elapsed_steps >= (index - 1) * 13 + self.failure_step - 1
            )
            if failure_reached:
                if not test["test_time"]:
                    test["test_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                test["state"] = "failed"
                test["step"] = self.failure_step
                test["error_code"] = "MOCK_STEP_FAILED"
                test["error_message"] = f"模拟失败：{STEP_NAMES[self.failure_step]}"
                self.batch["running"] = False
                self.batch["state"] = "failed"
                break
            if index <= target_completed:
                self._finish_test(test)
            elif index == target_completed + 1 and not (
                self.stop_after_sample is not None
                and index > self.stop_after_sample
            ):
                if not test["test_time"]:
                    test["test_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                test["state"] = "in_progress"
                test["step"] = active_step
                break

        finished = sum(item["state"] == "finished" for item in tests)
        failed = sum(item["state"] == "failed" for item in tests)
        self.batch["finished_count"] = finished
        self.batch["failed_count"] = failed
        active = next(
            (item for item in tests if item["state"] == "in_progress"), None
        )
        if active:
            self.batch["current_test"] = active["_sample_index"]
            self.batch["current_test_step"] = active["step"]
        if (
            self.stop_after_sample is not None
            and finished >= self.stop_after_sample
        ):
            self.batch["running"] = False
            self.batch["state"] = "stopped"
        elif finished + failed == len(tests):
            self.batch["running"] = False
            self.batch["state"] = "completed" if failed == 0 else "failed"
            self.batch["current_test"] = len(tests)
            self.batch["current_test_step"] = tests[-1]["step"] if tests else 0
        self.history[self.batch["batch_id"]] = copy.deepcopy(self.batch)

    @staticmethod
    def _finish_test(test: dict[str, Any]) -> None:
        index = int(test["_sample_index"])
        if not test["test_time"]:
            test["test_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        resistance_ohm = round(80.0 + index * 4.25, 3)
        h1_mm = 8.0
        h2_mm = round(8.8 + index * 0.02, 3)
        thickness_cm = (h2_mm - h1_mm) / 10.0
        ionic_conductivity_ms_cm = (
            thickness_cm / (resistance_ohm * DISC_AREA_CM2) * 1000.0
        )
        test.update(
            {
                "state": "finished",
                "step": 13,
                "bottle_code": f"LOT-260812-{index:03d}-CRU-S-{index:03d}-260812",
                "recipe": "R-202608-001",
                "formula": "Li6PS5Cl",
                "temperature": round(26.5 + index * 0.1, 2),
                "R": resistance_ohm,
                "h1": h1_mm,
                "h2": h2_mm,
                "area": DISC_AREA_CM2,
                "ion_conductivity": round(ionic_conductivity_ms_cm, 6),
            }
        )

    @staticmethod
    def _public(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: MockConductivityState._public(item)
                for key, item in value.items()
                if not key.startswith("_")
            }
        if isinstance(value, list):
            return [MockConductivityState._public(item) for item in value]
        return value

    def _month_prefix(self, month: str) -> str:
        text = str(month or "").strip()
        if not text:
            text = datetime.now().strftime("%Y-%m")
        return text.replace("-", "")[:6]

    def _remember(self, action: Any, param: dict[str, Any], response: dict[str, Any]) -> None:
        try:
            result = int(response.get("result") or 0)
        except (TypeError, ValueError):
            result = 7
        step = None
        if action == "manual_run":
            try:
                step = int(param.get("step") or 0)
            except (TypeError, ValueError):
                step = 0
        self.last_action = {
            "action": str(action or ""),
            "result": result,
            "step": step,
            "message": RESULT_MESSAGES.get(result, "工站返回错误"),
            "time": datetime.now().strftime("%H:%M:%S"),
        }

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("request_id")
        action = request.get("action")
        param = request.get("param") or {}
        if not isinstance(param, dict):
            param = {}
        with self.lock:
            self._update()
            response = self._handle_locked(request_id, action, param)
            self._remember(action, param, response)
            return response

    def _handle_locked(self, request_id: Any, action: Any, param: dict[str, Any]) -> dict[str, Any]:
        if action == "station_status":
            return self._response(request_id, data=dict(self.devices))
        if action == "material_status":
            return self._response(request_id, data=copy.deepcopy(self.materials))
        if action == "batch_status":
            if not self.batch:
                return self._response(request_id, data={"running": False, "batch": None})
            summary_keys = (
                "batch_id",
                "test_round",
                "bottle",
                "mold",
                "funnel",
                "current_test",
                "current_test_step",
                "finished_count",
                "failed_count",
                "state",
            )
            summary = {key: self.batch.get(key) for key in summary_keys}
            return self._response(
                request_id,
                data={"running": self.batch["running"], "batch": summary},
            )
        if action == "batch_result":
            batch_id = str(param.get("batch_id") or "")
            if not batch_id:
                batch = self.batch
            else:
                batch = self.history.get(batch_id)
            if not batch:
                return self._response(request_id, result=9)
            return self._response(
                request_id,
                data={
                    "batch": {
                        "batch_id": batch["batch_id"],
                        "test_round": batch["test_round"],
                        "state": batch["state"],
                        "tests": self._public(copy.deepcopy(batch["tests"])),
                    }
                },
            )
        if action == "query_batch":
            prefix = self._month_prefix(str(param.get("month") or ""))
            batch_ids = [
                batch_id
                for batch_id in self.history
                if str(batch_id).replace("-", "")[:6] == prefix
            ]
            return self._response(request_id, data={"batch_ids": batch_ids})
        if action == "start_batch":
            if self._plc_offline():
                return self._response(request_id, result=1)
            if self.batch and self.batch["running"]:
                return self._response(request_id, result=4)
            positions = [self._positions(self.materials[key]) for key in ("bottle", "mold", "funnel")]
            if not positions[0] or len({len(items) for items in positions}) != 1:
                return self._response(request_id, result=2)
            batch_id = datetime.now().strftime("%Y%m%d%H%M%S%f")[:17]
            tests = [
                self._new_test(index, bottle, mold, funnel)
                for index, (bottle, mold, funnel) in enumerate(zip(*positions), start=1)
            ]
            self.batch = {
                "batch_id": batch_id,
                "test_round": len(tests),
                "bottle": positions[0],
                "mold": positions[1],
                "funnel": positions[2],
                "current_test": 1,
                "current_test_step": 1,
                "finished_count": 0,
                "failed_count": 0,
                "running": True,
                "state": "running",
                "tests": tests,
            }
            self.started_monotonic = time.monotonic()
            self.stop_requested = False
            self.stop_after_sample = None
            self._manual_busy_until = 0.0
            self.history[batch_id] = copy.deepcopy(self.batch)
            return self._response(request_id, data={"batch_id": batch_id})
        if action == "stop_current_batch":
            if not self.batch or not self.batch["running"]:
                return self._response(request_id, result=3)
            self.stop_requested = True
            self.stop_after_sample = int(self.batch.get("current_test") or 1)
            self.batch["state"] = "stopping"
            if not self.auto_advance:
                self.batch["running"] = False
                self.batch["state"] = "stopped"
            return self._response(request_id)
        if action == "manual_run":
            if self._plc_offline():
                return self._response(request_id, result=1)
            if not self.batch or not self.batch["running"]:
                return self._response(request_id, result=3)
            if time.monotonic() < self._manual_busy_until:
                return self._response(request_id, result=6)
            step = int(param.get("step") or 0)
            if not 1 <= step <= 13:
                return self._response(request_id, result=5)
            active = next(
                (item for item in self.batch["tests"] if item["state"] == "in_progress"),
                self.batch["tests"][0],
            )
            active["state"] = "in_progress"
            active["step"] = step
            if not active["test_time"]:
                active["test_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.batch["current_test"] = active["_sample_index"]
            self.batch["current_test_step"] = step
            self._manual_busy_until = time.monotonic() + self.step_interval
            return self._response(
                request_id,
                data={
                    "batch_id": self.batch["batch_id"],
                    "test_time": active["test_time"],
                },
            )
        if action == "set_slot":
            try:
                layer = resolve_layer(str(param.get("layer") or ""))
                if param.get("index") is not None:
                    index = int(param.get("index"))
                else:
                    index = parse_slot_index(param.get("slot") or 0)
                self.set_slot(layer, index, bool(param.get("occupied")))
            except (TypeError, ValueError) as exc:
                return self._response(request_id, result=5, data={"error": str(exc)})
            return self._response(request_id, data=copy.deepcopy(self.materials))
        if action == "set_materials":
            for layer in RACK_LAYERS:
                raw = param.get(layer)
                if raw is None:
                    continue
                flags = [int(flag) for flag in list(raw)[:SLOT_COUNT]]
                flags.extend([0] * (SLOT_COUNT - len(flags)))
                self.materials[layer] = flags
            return self._response(request_id, data=copy.deepcopy(self.materials))
        if action == "clear_current_batch":
            if self.batch and self.batch["running"]:
                return self._response(request_id, result=4)
            self.batch = None
            self.started_monotonic = None
            self.stop_requested = False
            self.stop_after_sample = None
            self._manual_busy_until = 0.0
            return self._response(request_id)
        return self._response(request_id, result=8)

    @staticmethod
    def _response(
        request_id: Any, result: int = 0, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        response: dict[str, Any] = {"request_id": request_id, "result": result}
        if data is not None:
            response["data"] = data
        return response


class _Handler(socketserver.StreamRequestHandler):
    def setup(self) -> None:
        super().setup()
        server: MockConductivityServer = self.server  # type: ignore[assignment]
        with server.client_lock:
            server.client_count += 1
            server.last_client = f"{self.client_address[0]}:{self.client_address[1]}"
            server.state.last_client = server.last_client

    def finish(self) -> None:
        server: MockConductivityServer = self.server  # type: ignore[assignment]
        with server.client_lock:
            server.client_count = max(0, server.client_count - 1)
        super().finish()

    def handle(self) -> None:
        state: MockConductivityState = self.server.state  # type: ignore[attr-defined]
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            request: dict[str, Any] | None = None
            try:
                parsed = json.loads(raw.decode("utf-8").strip())
                if not isinstance(parsed, dict):
                    raise ValueError("请求必须是 JSON 对象")
                request = parsed
                response = state.handle(request)
            except Exception as exc:  # noqa: BLE001
                request_id = request.get("request_id") if isinstance(request, dict) else None
                response = {"request_id": request_id, "result": 7, "message": str(exc)}
            payload = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
            self.wfile.write((payload + "\r\n").encode("utf-8"))
            self.wfile.flush()


class MockConductivityServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int] = ("127.0.0.1", 19091),
        *,
        state: MockConductivityState | None = None,
    ) -> None:
        self.state = state or MockConductivityState()
        self.client_lock = threading.Lock()
        self.client_count = 0
        self.last_client = ""
        super().__init__(address, _Handler)


class MockConductivityRuntime:
    """TCP Mock 及其可选 HTTP 交互页。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 19091,
        *,
        state: MockConductivityState | None = None,
        ui_host: str = "127.0.0.1",
        ui_port: int = 0,
        enable_ui: bool = False,
    ) -> None:
        self.state = state or MockConductivityState()
        self._tcp_host = host
        self._ui_host = ui_host
        self._enable_ui = enable_ui
        self._lock = threading.Lock()
        self.tcp_server = MockConductivityServer((host, port), state=self.state)
        self.http_server: ThreadingHTTPServer | None = None
        self._threads: list[threading.Thread] = []
        if enable_ui:
            self.http_server = ThreadingHTTPServer(
                (ui_host, ui_port), _make_ui_handler(self)
            )

    @property
    def host(self) -> str:
        return str(self.tcp_server.server_address[0])

    @property
    def port(self) -> int:
        return int(self.tcp_server.server_address[1])

    @property
    def ui_port(self) -> int:
        if self.http_server is None:
            return 0
        return int(self.http_server.server_address[1])

    def start(self) -> None:
        tcp_thread = threading.Thread(
            target=self.tcp_server.serve_forever,
            name="mock-conductivity-tcp",
            daemon=True,
        )
        tcp_thread.start()
        self._threads.append(tcp_thread)
        if self.http_server is not None:
            http_thread = threading.Thread(
                target=self.http_server.serve_forever,
                name="mock-conductivity-ui",
                daemon=True,
            )
            http_thread.start()
            self._threads.append(http_thread)

    def rebind(self, host: str, port: int) -> tuple[str, int]:
        with self._lock:
            old = self.tcp_server
            old.shutdown()
            old.server_close()
            self.tcp_server = MockConductivityServer((host, port), state=self.state)
            thread = threading.Thread(
                target=self.tcp_server.serve_forever,
                name="mock-conductivity-tcp",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)
            return self.host, self.port

    def close(self) -> None:
        self.tcp_server.shutdown()
        self.tcp_server.server_close()
        if self.http_server is not None:
            self.http_server.shutdown()
            self.http_server.server_close()
        for thread in self._threads:
            thread.join(timeout=2)
        self._threads.clear()

    def status_payload(self) -> dict[str, Any]:
        snap = self.state.snapshot()
        return {
            "tcp_host": self.host,
            "tcp_port": self.port,
            "ui_port": self.ui_port,
            "listening": True,
            "client_count": self.tcp_server.client_count,
            "last_client": snap.get("last_client") or self.tcp_server.last_client,
            **snap,
        }


def _make_ui_handler(runtime: MockConductivityRuntime) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _send(self, body: bytes, content_type: str, code: int = 200) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: Any, code: int = 200) -> None:
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self._send(payload, "application/json; charset=utf-8", code)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            value = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(value, dict):
                raise ValueError("请求体必须是对象")
            return value

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path in {"/", "/index.html"}:
                self._send(_ui_page().encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/api/state":
                self._json(runtime.status_payload())
                return
            self._json({"error": "not found"}, 404)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            try:
                if path == "/api/slot":
                    body = self._read_json()
                    runtime.state.set_slot(
                        str(body.get("layer") or ""),
                        int(body.get("index") or 0),
                        bool(body.get("occupied")),
                    )
                    self._json(runtime.status_payload())
                    return
                if path == "/api/device":
                    body = self._read_json()
                    runtime.state.set_device(
                        str(body.get("key") or ""),
                        bool(body.get("online")),
                    )
                    self._json(runtime.status_payload())
                    return
                if path == "/api/materials":
                    query = parse_qs(urlparse(self.path).query)
                    action = str((query.get("action") or ["clear"])[0])
                    if action == "demo":
                        runtime.state.load_demo_materials()
                    else:
                        runtime.state.clear_materials()
                    self._json(runtime.status_payload())
                    return
                if path == "/api/bind":
                    body = self._read_json()
                    host = str(body.get("host") or runtime.host)
                    port = int(body.get("port") or runtime.port)
                    runtime.rebind(host, port)
                    self._json(runtime.status_payload())
                    return
                if path == "/api/reset":
                    runtime.state.reset()
                    self._json(runtime.status_payload())
                    return
                if path == "/api/auto":
                    body = self._read_json()
                    runtime.state.set_auto_advance(bool(body.get("enabled")))
                    self._json(runtime.status_payload())
                    return
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 400)
                return
            self._json({"error": "not found"}, 404)

    return Handler


def _ui_page() -> str:
    device_rows = "".join(
        f'<label><input type="checkbox" data-device="{key}"> {DEVICE_LABELS[key]} <small>{key}</small></label>'
        for key in DEVICE_KEYS
    )
    racks = []
    for layer in RACK_LAYERS:
        cells = "".join(
            f'<button type="button" class="slot" data-layer="{layer}" data-index="{index}">'
            f"#{index + 1}<small>{SLOT_LABELS[index]}</small></button>"
            for index in range(SLOT_COUNT)
        )
        racks.append(
            f'<section class="card"><h2>{LAYER_LABELS[layer]} '
            f'<span id="count-{layer}">在位 0/10</span></h2>'
            f'<div class="grid">{cells}</div></section>'
        )
    rack_html = "".join(racks)
    step_html = "".join(
        f'<li data-step="{step}">{step}. {name}</li>'
        for step, name in STEP_NAMES.items()
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>电导工站 Mock</title>
  <style>
    :root {{ color-scheme: dark; font-family: "Microsoft YaHei", sans-serif; }}
    body {{ margin: 0; background: #0d1524; color: #e8eef8; }}
    header {{ padding: 16px 24px; background: #132038; border-bottom: 1px solid #25324a; }}
    main {{ max-width: 980px; margin: 20px auto; padding: 0 16px 48px; }}
    .card {{ background: #101b2e; border: 1px solid #25324a; border-radius: 12px; padding: 16px; margin-bottom: 16px; }}
    .grid {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 8px; }}
    .slot {{ padding: 12px 6px; border-radius: 8px; border: 1px solid #314a63; background: #1a2740; color: #9fb0c9; cursor: pointer; }}
    .slot small {{ display: block; color: #6f83a2; }}
    .slot.on {{ background: #1f6a46; color: #d7ffe8; border-color: #42d883; }}
    .row {{ display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }}
    input[type=text], input[type=number] {{ background: #0d1524; color: #e8eef8; border: 1px solid #314a63; padding: 6px 8px; border-radius: 6px; }}
    button.action {{ background: #2d6cdf; color: white; border: 0; border-radius: 8px; padding: 8px 12px; cursor: pointer; }}
    .ok {{ color: #72e6a1; }} .warn {{ color: #ffd88a; }} .err {{ color: #ff8a8a; }}
    .steps {{ list-style: none; padding: 0; margin: 12px 0 0; display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }}
    .steps li {{ padding: 8px 10px; border-radius: 8px; border: 1px solid #314a63; background: #1a2740; color: #9fb0c9; }}
    .steps li.on {{ background: #1f6a46; color: #d7ffe8; border-color: #42d883; }}
    .steps li.done {{ border-color: #3d6d55; color: #8fd4b0; }}
  </style>
</head>
<body>
  <header>
    <h1>电导工站虚拟服务</h1>
    <p id="listen">监听中…</p>
  </header>
  <main>
    <section class="card">
      <h2>连接</h2>
      <div class="row">
        <label>地址 <input id="host" type="text"></label>
        <label>端口 <input id="port" type="number"></label>
        <button class="action" id="bind">重新绑定并确认监听</button>
        <span id="clients"></span>
      </div>
      <p id="last-action"></p>
    </section>
    <section class="card">
      <h2>批次与 13 步</h2>
      <p id="batch-line">无批次。备料后发 start_batch 即整批自动实验，不必串联 13 步。</p>
      <p class="row">
        <label><input type="checkbox" id="auto-advance"> 自动推进（start_batch 整批实验用；手动单步时请关闭）</label>
      </p>
      <ol id="steps" class="steps">{step_html}</ol>
    </section>
    <section class="card">
      <h2>设备在线</h2>
      <div class="row" id="devices">{device_rows}</div>
    </section>
    {rack_html}
    <section class="card row">
      <button class="action" id="clear">清空库位</button>
      <button class="action" id="demo">装入演示 3+3+3</button>
      <button class="action" id="reset">初始化 / 清零</button>
    </section>
  </main>
  <script>
    async function load() {{
      const state = await (await fetch("/api/state")).json();
      document.getElementById("host").value = state.tcp_host;
      document.getElementById("port").value = state.tcp_port;
      document.getElementById("listen").innerHTML =
        'TCP <span class="ok">' + state.tcp_host + ':' + state.tcp_port + '</span> 已监听';
      const clients = document.getElementById("clients");
      if (state.client_count) {{
        clients.textContent = 'UniLab 在线 ' + state.last_client;
        clients.className = "ok";
      }} else if (state.last_client || (state.last_action && state.last_action.time)) {{
        clients.textContent = '最近客户端 ' + (state.last_client || '已通信');
        clients.className = "ok";
      }} else {{
        clients.textContent = '尚无客户端';
        clients.className = "warn";
      }}
      const last = state.last_action;
      const lastEl = document.getElementById("last-action");
      if (last) {{
        const stepText = last.step ? (' step=' + last.step) : '';
        lastEl.textContent = last.time + '  ' + last.action + stepText +
          '  result=' + last.result + '  ' + (last.message || '');
        lastEl.className = Number(last.result) === 0 ? "ok" : "err";
      }} else {{
        lastEl.textContent = '';
      }}
      const batchLine = document.getElementById("batch-line");
      if (state.running) {{
        batchLine.innerHTML = '批次 <span class="ok">' + (state.batch_id || '') +
          '</span> 运行中 · 样品 ' + state.current_test +
          ' · 步骤 ' + state.current_test_step + ' ' + (state.current_step_name || '') +
          ' · 完成 ' + state.finished_count + ' / 失败 ' + state.failed_count;
      }} else if (state.batch_id) {{
        batchLine.textContent = '批次 ' + state.batch_id + ' ' + (state.batch_state || '已结束') +
          ' · 完成 ' + state.finished_count + ' / 失败 ' + state.failed_count;
      }} else {{
        batchLine.textContent = '无批次。备料后在 UniLab 发 start_batch 即可整批自动跑 1–13 步，不必串联分步。';
      }}
      const autoBox = document.getElementById("auto-advance");
      if (autoBox && document.activeElement !== autoBox) {{
        autoBox.checked = Boolean(state.auto_advance);
      }}
      const current = Number(state.current_test_step || 0);
      for (const el of document.querySelectorAll("#steps li")) {{
        const n = Number(el.dataset.step);
        el.classList.toggle("on", Boolean(state.running) && n === current);
        el.classList.toggle("done", current > 0 && n < current);
      }}
      for (const box of document.querySelectorAll("[data-device]")) {{
        box.checked = Number(state.devices[box.dataset.device] || 0) === 1;
      }}
      for (const layer of Object.keys(state.materials || {{}})) {{
        const values = state.materials[layer];
        const filled = values.filter(v => Number(v) === 1).length;
        const count = document.getElementById("count-" + layer);
        if (count) count.textContent = "在位 " + filled + "/10";
        values.forEach((flag, index) => {{
          const btn = document.querySelector('.slot[data-layer="'+layer+'"][data-index="'+index+'"]');
          if (btn) btn.classList.toggle("on", Number(flag) === 1);
        }});
      }}
    }}
    document.addEventListener("click", async (event) => {{
      const slot = event.target.closest(".slot");
      if (slot) {{
        await fetch("/api/slot", {{
          method: "POST",
          headers: {{"Content-Type": "application/json"}},
          body: JSON.stringify({{
            layer: slot.dataset.layer,
            index: Number(slot.dataset.index),
            occupied: !slot.classList.contains("on")
          }})
        }});
        return load();
      }}
    }});
    document.getElementById("devices").addEventListener("change", async (event) => {{
      const box = event.target;
      if (!box.dataset.device) return;
      await fetch("/api/device", {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify({{key: box.dataset.device, online: box.checked}})
      }});
      load();
    }});
    document.getElementById("bind").onclick = async () => {{
      await fetch("/api/bind", {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify({{
          host: document.getElementById("host").value,
          port: Number(document.getElementById("port").value)
        }})
      }});
      load();
    }};
    document.getElementById("clear").onclick = async () => {{
      await fetch("/api/materials?action=clear", {{method: "POST"}});
      load();
    }};
    document.getElementById("demo").onclick = async () => {{
      await fetch("/api/materials?action=demo", {{method: "POST"}});
      load();
    }};
    document.getElementById("reset").onclick = async () => {{
      await fetch("/api/reset", {{method: "POST"}});
      load();
    }};
    document.getElementById("auto-advance").onchange = async (event) => {{
      await fetch("/api/auto", {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify({{enabled: event.target.checked}})
      }});
      load();
    }};
    load();
    setInterval(load, 2000);
  </script>
</body>
</html>"""


def start_mock_in_process(
    host: str = "127.0.0.1",
    port: int = 19091,
    *,
    state: MockConductivityState | None = None,
    enable_ui: bool = False,
    ui_port: int = 0,
) -> MockConductivityRuntime:
    """在后台线程启动 TCP Mock，供 ConductivityStation(use_mock=True) 使用。"""
    runtime = MockConductivityRuntime(
        host,
        port,
        state=state,
        enable_ui=enable_ui,
        ui_port=ui_port,
    )
    runtime.start()
    return runtime


def main() -> None:
    parser = argparse.ArgumentParser(description="电导工站 TCP 协议模拟服务端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19091)
    parser.add_argument("--ui-host", default="127.0.0.1")
    parser.add_argument("--ui-port", type=int, default=19092)
    parser.add_argument("--no-ui", action="store_true")
    parser.add_argument("--empty", action="store_true", help="库位全空（默认，便于模拟人工备料）")
    parser.add_argument("--demo", action="store_true", help="启动时装入演示 3 瓶 / 3 模 / 3 漏斗")
    parser.add_argument("--step-interval", type=float, default=0.5)
    parser.add_argument(
        "--no-auto-advance",
        action="store_true",
        help="关闭自动推进，便于手动单步；默认开启，start_batch 会整批跑完",
    )
    parser.add_argument("--failure-sample", type=int)
    parser.add_argument("--failure-step", type=int, default=10)
    args = parser.parse_args()
    state = MockConductivityState(
        step_interval=args.step_interval,
        failure_sample=args.failure_sample,
        failure_step=args.failure_step,
        materials=demo_loaded_materials() if args.demo else empty_materials(),
        auto_advance=not args.no_auto_advance,
    )
    runtime = start_mock_in_process(
        args.host,
        args.port,
        state=state,
        enable_ui=not args.no_ui,
        ui_port=args.ui_port,
    )
    listen = f"模拟电导工站 TCP {runtime.host}:{runtime.port}"
    if runtime.ui_port:
        listen += f"  交互页 http://{args.ui_host}:{runtime.ui_port}/"
    print(listen, flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
