"""电导率自动化工站 TCP 客户端及 UniLab 动作。

合作方工站作为 TCP 服务端，UniLab 作为客户端。报文为 UTF-8 JSON，使用
CRLF 分帧。查询类动作允许在连接中断后重连重试一次；启动、停止和手动运行
属于有副作用的命令，发送结果不明确时不会自动重发，避免重复执行。
"""

from __future__ import annotations

import json
import socket
import threading
import time
from datetime import datetime
from itertools import count
from typing import Any

from unilabos.registry.decorators import action, device, not_action, topic_config

from yb_sse_devices.protocol import (
    DEVICE_KEYS,
    HEALTH_LABELS,
    LAYER_WAREHOUSE_NAMES,
    RACK_LAYERS,
    SLOT_LABELS,
    demo_loaded_materials,
    empty_materials,
    occupied_count,
    parse_slot_index,
    protocol_error_message,
    resolve_layer,
    summarize_station_health,
)

QUERY_CACHE_TTL_S = 0.3


class ConductivityStationTransportError(RuntimeError):
    """TCP 连接或报文传输失败。"""


class ConductivityStationProtocolError(RuntimeError):
    """服务端响应不符合电导工站协议。"""


@device(
    id="conductivity_station",
    category=["workstation", "conductivity"],
    display_name="电导率自动化测试工站",
    description="通过 TCP JSON/CRLF 协议控制电导率自动化测试工站",
    version="1.0.0",
)
class ConductivityStation:
    """电导工站长连接客户端。"""

    def __init__(
        self,
        device_id: str | None = None,
        config: dict[str, Any] | None = None,
        ip: str = "127.0.0.1",
        port: int = 19091,
        connect_timeout: float = 5.0,
        response_timeout: float = 10.0,
        max_message_bytes: int = 4194304,
        encoding: str = "utf-8",
        frame_delimiter: str = "\\r\\n",
        station_action_names: dict[str, str] | None = None,
        use_mock: bool = False,
        occupancy_poll_interval: float = 30.0,
        load_demo_occupancy: bool = False,
        occupancy: dict[str, list[int]] | None = None,
        **_: Any,
    ) -> None:
        """初始化电导工站客户端。

        Args:
            device_id[设备 ID]: Uni-Lab 设备实例 ID。
            config[设备配置]: 模板标准配置字典；其中的连接参数优先于同名默认参数。
            ip[工站 IP]: 电导工站 TCP 服务端地址。
            port[工站端口]: 电导工站 TCP 服务端端口。
            connect_timeout[连接超时]: 建立 TCP 连接的超时秒数。
            response_timeout[响应超时]: 等待工站响应的超时秒数。
            max_message_bytes[最大报文长度]: 单个 JSON 报文允许的最大字节数。
            encoding[字符编码]: TCP JSON 报文使用的字符编码。
            frame_delimiter[分帧符]: 每个 JSON 报文结尾使用的分隔符。
            station_action_names[动作名映射]: Uni-Lab 动作名到现场协议动作名的映射。
            use_mock[进程内 Mock]: 为 True 时在本进程启动 TCP 模拟工站。
            occupancy_poll_interval[占用轮询秒]: 主动查 2.2 并刷新库位的间隔；0 表示关闭。
            load_demo_occupancy[演示占位]: 启动时写入演示 3+3+3 占位（虚拟机）。
            occupancy[初始占位]: 三层 0/1 数组，写入设备图即可预设库位。
        """
        resolved_config = dict(config or {})
        self.device_id = device_id or "conductivity_station"
        self.config = resolved_config
        self.ip = str(resolved_config.get("ip", ip))
        self.port = int(resolved_config.get("port", port))
        self.connect_timeout = float(
            resolved_config.get("connect_timeout", connect_timeout)
        )
        self.response_timeout = float(
            resolved_config.get("response_timeout", response_timeout)
        )
        self.max_message_bytes = int(
            resolved_config.get("max_message_bytes", max_message_bytes)
        )
        self.encoding = (
            str(resolved_config.get("encoding", encoding)).strip() or "utf-8"
        )
        frame_delimiter = str(
            resolved_config.get("frame_delimiter", frame_delimiter)
        )
        delimiter_text = (
            str(frame_delimiter).replace("\\r", "\r").replace("\\n", "\n")
        )
        self.frame_delimiter = delimiter_text.encode(self.encoding)
        if not self.frame_delimiter:
            raise ValueError("frame_delimiter 不能为空")
        configured_action_names = resolved_config.get(
            "station_action_names", station_action_names or {}
        )
        self.station_action_names = {
            str(key): str(value)
            for key, value in configured_action_names.items()
            if str(key) and str(value)
        }
        self.use_mock = bool(resolved_config.get("use_mock", use_mock))
        self.occupancy_poll_interval = max(
            0.0,
            float(
                resolved_config.get(
                    "occupancy_poll_interval", occupancy_poll_interval
                )
            ),
        )
        self.load_demo_occupancy = bool(
            resolved_config.get("load_demo_occupancy", load_demo_occupancy)
        )
        raw_occupancy = resolved_config.get("occupancy", occupancy)
        self._initial_occupancy = (
            self._normalize_occupancy(raw_occupancy)
            if isinstance(raw_occupancy, dict) and raw_occupancy
            else None
        )
        self._sock: socket.socket | None = None
        self._recv_buffer = bytearray()
        self._request_ids = count(1)
        self._lock = threading.RLock()
        self._query_cache: dict[str, tuple[float, Any]] = {}
        self._connected = False
        self._materials = empty_materials()
        self._devices = {key: 0 for key in DEVICE_KEYS}
        self.deck: Any = resolved_config.get("deck")
        self._ros_node: Any = None
        self._last_occupancy: tuple[tuple[int, ...], ...] | None = None
        self._mock_runtime: Any = None
        self._poll_stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        if self.use_mock:
            from yb_sse_devices.mock_server import start_mock_in_process

            self._mock_runtime = start_mock_in_process(self.ip, self.port)
            self.port = int(self._mock_runtime.port)
        self._ensure_deck()

    @not_action
    def close(self) -> None:
        """关闭当前 TCP 连接，并停止本进程启动的 Mock。"""
        self._poll_stop.set()
        thread, self._poll_thread = self._poll_thread, None
        if thread is not None:
            thread.join(timeout=2)
        with self._lock:
            self._disconnect()
            runtime, self._mock_runtime = self._mock_runtime, None
        if runtime is not None:
            runtime.close()

    @not_action
    def attach_deck(self, deck: Any) -> None:
        """挂上三层料架 Deck，后续占用刷新会写入对应库位。"""
        self.deck = deck
        self._ensure_deck()
        self._sync_deck_occupancy(self._materials)

    def post_init(self, ros_node: Any) -> None:
        """UniLab 节点就绪后把三层料架挂进资源树，仪器耗材才能看到库位。"""
        self._ros_node = ros_node
        self._ensure_deck()
        tracker = getattr(ros_node, "resource_tracker", None)
        if tracker is not None and self.deck is not None:
            try:
                tracker.add_resource(self.deck)
            except Exception:
                pass
        if self._initial_occupancy is not None:
            self._apply_occupancy(self._initial_occupancy)
        elif self.load_demo_occupancy:
            self._apply_occupancy(demo_loaded_materials())
        self._refresh_materials()
        self._push_deck()
        self._start_occupancy_poller()

    def _start_occupancy_poller(self) -> None:
        if self.occupancy_poll_interval <= 0 or self._poll_thread is not None:
            return
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(
            target=self._occupancy_poll_loop,
            name=f"{self.device_id}-occupancy-poll",
            daemon=True,
        )
        self._poll_thread.start()

    def _occupancy_poll_loop(self) -> None:
        while not self._poll_stop.wait(self.occupancy_poll_interval):
            if self.occupancy_poll_interval <= 0:
                continue
            try:
                self._query_cache.pop("material_status", None)
                self._refresh_materials()
                self._push_deck()
            except Exception:
                continue

    def _ensure_deck(self) -> Any:
        deck = self.deck
        if deck is None:
            from yb_sse_devices.resources.decks import ConductivityStation_Deck

            deck = ConductivityStation_Deck(
                name=f"{self.device_id}_Deck",
                setup=True,
            )
            self.deck = deck
        elif not getattr(deck, "warehouses", None) and hasattr(deck, "setup"):
            try:
                deck.setup()
            except Exception:
                pass
        return deck

    def _push_deck(self) -> None:
        ros_node = self._ros_node
        deck = self.deck
        if ros_node is None or deck is None:
            return
        try:
            from unilabos.ros.nodes.base_device_node import ROS2DeviceNode

            ROS2DeviceNode.run_async_func(
                ros_node.update_resource, True, resources=[deck]
            )
        except Exception:
            return

    def _require_ok(self, response: dict[str, Any]) -> dict[str, Any]:
        try:
            result = int(response.get("result"))
        except (TypeError, ValueError):
            raise ConductivityStationProtocolError("响应 result 无效") from None
        if result != 0:
            raise ConductivityStationProtocolError(protocol_error_message(result))
        return response

    def _normalize_occupancy(self, raw: dict[str, Any] | None) -> dict[str, list[int]]:
        snapshot = empty_materials()
        for key, values in (raw or {}).items():
            try:
                layer = resolve_layer(str(key))
            except ValueError:
                continue
            flags = [int(flag) for flag in list(values or [])[: len(SLOT_LABELS)]]
            flags.extend([0] * (len(SLOT_LABELS) - len(flags)))
            snapshot[layer] = flags
        return snapshot

    def _apply_occupancy(self, occupancy: dict[str, Any]) -> dict[str, list[int]]:
        snapshot = self._normalize_occupancy(occupancy)
        self._materials = snapshot
        self._sync_deck_occupancy(snapshot)
        try:
            response = self._request("set_materials", snapshot)
            if int(response.get("result") or 8) == 0:
                self._query_cache.pop("material_status", None)
        except Exception:
            pass
        self._push_deck()
        return snapshot

    def _disconnect(self) -> None:
        sock, self._sock = self._sock, None
        self._recv_buffer.clear()
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _connect(self) -> socket.socket:
        if self._sock is None:
            try:
                sock = socket.create_connection(
                    (self.ip, self.port), timeout=self.connect_timeout
                )
                sock.settimeout(self.response_timeout)
                self._sock = sock
            except OSError as exc:
                self._disconnect()
                raise ConductivityStationTransportError(
                    f"无法连接电导工站 {self.ip}:{self.port}: {exc}"
                ) from exc
        return self._sock

    def _read_frame(self, sock: socket.socket) -> bytes:
        delimiter = self.frame_delimiter
        while True:
            marker = self._recv_buffer.find(delimiter)
            if marker >= 0:
                frame = bytes(self._recv_buffer[:marker])
                del self._recv_buffer[: marker + len(delimiter)]
                return frame
            if len(self._recv_buffer) > self.max_message_bytes:
                raise ConductivityStationProtocolError(
                    f"响应超过最大长度 {self.max_message_bytes} 字节"
                )
            try:
                chunk = sock.recv(65536)
            except (OSError, socket.timeout) as exc:
                raise ConductivityStationTransportError(
                    f"等待电导工站响应失败: {exc}"
                ) from exc
            if not chunk:
                raise ConductivityStationTransportError("电导工站在响应前关闭连接")
            self._recv_buffer.extend(chunk)

    def _exchange(self, request: dict[str, Any]) -> dict[str, Any]:
        sock = self._connect()
        encoded = json.dumps(
            request, ensure_ascii=False, separators=(",", ":")
        ).encode(self.encoding) + self.frame_delimiter
        if len(encoded) > self.max_message_bytes:
            raise ConductivityStationProtocolError(
                f"请求超过最大长度 {self.max_message_bytes} 字节"
            )
        try:
            sock.sendall(encoded)
            frame = self._read_frame(sock)
        except Exception:
            self._disconnect()
            raise
        try:
            response = json.loads(frame.decode(self.encoding))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._disconnect()
            raise ConductivityStationProtocolError(
                f"响应不是有效 {self.encoding} JSON: {exc}"
            ) from exc
        if not isinstance(response, dict):
            raise ConductivityStationProtocolError("响应必须是 JSON 对象")
        if response.get("request_id") != request["request_id"]:
            raise ConductivityStationProtocolError(
                "响应 request_id 不匹配: "
                f"期望 {request['request_id']}，实际 {response.get('request_id')}"
            )
        if "result" not in response:
            raise ConductivityStationProtocolError("响应缺少 result 字段")
        return response

    def _request(
        self,
        action_name: str,
        param: dict[str, Any] | None = None,
        *,
        retry_read: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            request: dict[str, Any] = {
                "request_id": next(self._request_ids),
                "action": self.station_action_names.get(action_name, action_name),
            }
            if param is not None:
                request["param"] = param
            attempts = 2 if retry_read else 1
            for attempt in range(attempts):
                try:
                    return self._exchange(request)
                except ConductivityStationTransportError:
                    if attempt + 1 >= attempts:
                        raise
                    self._disconnect()
            raise AssertionError("unreachable")

    def _cached_query(self, cache_key: str, action_name: str) -> dict[str, Any] | None:
        now = time.monotonic()
        cached = self._query_cache.get(cache_key)
        if cached and now - cached[0] < QUERY_CACHE_TTL_S:
            return cached[1]
        try:
            response = self._request(action_name, retry_read=True)
        except ConductivityStationTransportError:
            self._connected = False
            self._query_cache.pop(cache_key, None)
            return None
        data = response.get("data") if isinstance(response.get("data"), dict) else {}
        self._connected = True
        self._query_cache[cache_key] = (now, data)
        return data

    def _refresh_station_devices(self) -> dict[str, int]:
        data = self._cached_query("station_status", "station_status")
        if data is None:
            self._devices = {key: 0 for key in DEVICE_KEYS}
            return self._devices
        self._devices = {key: int(data.get(key, 0) or 0) for key in DEVICE_KEYS}
        return self._devices

    @staticmethod
    def _is_resource_holder(item: Any) -> bool:
        if item is None:
            return False
        if type(item).__name__ == "ResourceHolder":
            return True
        try:
            from pylabrobot.resources import ResourceHolder

            return isinstance(item, ResourceHolder)
        except Exception:
            return False

    def _warehouse_slot(self, warehouse: Any, label: str) -> Any:
        try:
            return warehouse[label]
        except Exception:
            sites = getattr(warehouse, "sites", None)
            if isinstance(sites, dict):
                return sites.get(label)
            return None

    def _set_warehouse_slot(self, warehouse: Any, label: str, resource: Any) -> None:
        """对齐 Bioyond graphio：warehouse[槽位] = 物料，前端才认占用。"""
        current = self._warehouse_slot(warehouse, label)
        if resource is None:
            if current is None or self._is_resource_holder(current):
                if self._is_resource_holder(current):
                    nested = getattr(current, "resource", None)
                    if nested is not None:
                        unassign = getattr(current, "unassign_child_resource", None)
                        if callable(unassign):
                            unassign(nested)
                        else:
                            current.resource = None
                return
            try:
                warehouse[label] = None
            except Exception:
                if isinstance(getattr(warehouse, "sites", None), dict):
                    warehouse.sites[label] = None
            return
        if current is not None and not self._is_resource_holder(current):
            return
        try:
            warehouse[label] = resource
        except Exception:
            if isinstance(getattr(warehouse, "sites", None), dict):
                warehouse.sites[label] = resource

    def _sync_deck_occupancy(self, materials: dict[str, list[int]]) -> None:
        from yb_sse_devices.resources.materials import LAYER_RESOURCE_CLASSES

        deck = self._ensure_deck()
        warehouses = getattr(deck, "warehouses", None) if deck is not None else None
        if not isinstance(warehouses, dict) or not warehouses:
            return
        occupancy = tuple(
            tuple(int(flag) for flag in materials.get(layer, [])[: len(SLOT_LABELS)])
            for layer in RACK_LAYERS
        )
        changed = occupancy != self._last_occupancy
        mutated = False
        for layer, values in materials.items():
            warehouse = warehouses.get(LAYER_WAREHOUSE_NAMES.get(layer, ""))
            if warehouse is None:
                continue
            resource_cls = LAYER_RESOURCE_CLASSES[layer]
            for index, occupied in enumerate(values[: len(SLOT_LABELS)]):
                label = SLOT_LABELS[index]
                current = self._warehouse_slot(warehouse, label)
                filled = current is not None and not self._is_resource_holder(current)
                if int(occupied):
                    if not filled:
                        self._set_warehouse_slot(
                            warehouse, label, resource_cls(f"{layer}_{label}")
                        )
                        mutated = True
                elif filled:
                    self._set_warehouse_slot(warehouse, label, None)
                    mutated = True
        self._last_occupancy = occupancy
        if changed or mutated:
            self._push_deck()

    def _refresh_materials(self) -> dict[str, list[int]]:
        data = self._cached_query("material_status", "material_status")
        if data is None:
            self._materials = empty_materials()
            self._sync_deck_occupancy(self._materials)
            return self._materials
        snapshot = empty_materials()
        for layer in snapshot:
            raw = data.get(layer) or []
            snapshot[layer] = [int(flag) for flag in list(raw)[:10]]
            snapshot[layer].extend([0] * (10 - len(snapshot[layer])))
        self._materials = snapshot
        self._sync_deck_occupancy(snapshot)
        return snapshot

    def _refresh_batch(self) -> dict[str, Any]:
        data = self._cached_query("batch_status", "batch_status")
        if data is None:
            return {"running": False, "batch": None}
        return data

    def _device_flag(self, key: str) -> int:
        return int(self._refresh_station_devices().get(key, 0))

    @property
    @topic_config(period=2.0)
    def status(self) -> str:
        """工站状态：IDLE / BUSY / FAULT / OFFLINE。"""
        devices = self._refresh_station_devices()
        batch_running = False
        try:
            batch_running = bool(self._refresh_batch().get("running"))
        except Exception:
            batch_running = False
        return summarize_station_health(
            devices, connected=self._connected, batch_running=batch_running
        )

    @property
    @topic_config(period=2.0)
    def station_health(self) -> str:
        """工站状态中文：空闲 / 运行中 / 故障 / 离线。"""
        return HEALTH_LABELS.get(self.status, self.status)

    @property
    @topic_config()
    def connected(self) -> bool:
        """最近一次 2.1 查询是否成功。"""
        self._refresh_station_devices()
        return self._connected

    @property
    @topic_config()
    def robot_arm(self) -> int:
        """机械臂在线状态，1 在线 / 0 未在线。"""
        return self._device_flag("robot_arm")

    @property
    @topic_config()
    def scanner(self) -> int:
        """扫码器在线状态，1 在线 / 0 未在线。"""
        return self._device_flag("scanner")

    @property
    @topic_config()
    def lid_open_close_mechanism(self) -> int:
        """开合盖机构在线状态，1 在线 / 0 未在线。"""
        return self._device_flag("lid_open_close_mechanism")

    @property
    @topic_config()
    def powder_adding_mechanism(self) -> int:
        """加粉机构在线状态，1 在线 / 0 未在线。"""
        return self._device_flag("powder_adding_mechanism")

    @property
    @topic_config()
    def tablet_pressing_mechanism(self) -> int:
        """压片机构在线状态，1 在线 / 0 未在线。"""
        return self._device_flag("tablet_pressing_mechanism")

    @property
    @topic_config()
    def electrochemical_workstation(self) -> int:
        """电化学工作站在线状态，1 在线 / 0 未在线。"""
        return self._device_flag("electrochemical_workstation")

    @property
    @topic_config()
    def stack_rack(self) -> int:
        """堆栈料架在线状态，1 在线 / 0 未在线。"""
        return self._device_flag("stack_rack")

    @property
    @topic_config(period=5.0)
    def funnel_remaining(self) -> int:
        """待用漏斗数，在位格数 0–10。"""
        return int(occupied_count(self._refresh_materials().get("funnel")))

    @property
    @topic_config(period=5.0)
    def pending_bottles(self) -> int:
        """待测烧结料瓶数，在位格数 0–10。"""
        return int(occupied_count(self._refresh_materials().get("bottle")))

    @property
    @topic_config(period=5.0)
    def pending_molds(self) -> int:
        """待测模具数，在位格数 0–10。"""
        return int(occupied_count(self._refresh_materials().get("mold")))

    @property
    @topic_config()
    def batch_running(self) -> bool:
        """是否有批次正在运行。"""
        return bool(self._refresh_batch().get("running"))

    @property
    @topic_config()
    def current_test_step(self) -> int:
        """当前样品步骤 1–13；无批次时为 0。"""
        batch = self._refresh_batch().get("batch") or {}
        if not self._refresh_batch().get("running") and not batch:
            return 0
        try:
            return int(batch.get("current_test_step") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    @topic_config()
    def finished_count(self) -> int:
        """当前批次已完成样品数。"""
        batch = self._refresh_batch().get("batch") or {}
        try:
            return int(batch.get("finished_count") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    @topic_config()
    def failed_count(self) -> int:
        """当前批次失败样品数。"""
        batch = self._refresh_batch().get("batch") or {}
        try:
            return int(batch.get("failed_count") or 0)
        except (TypeError, ValueError):
            return 0

    @action(always_free=True, description="查询电导工站各机构在线状态")
    def station_status(self) -> dict[str, Any]:
        return self._request("station_status", retry_read=True)

    @action(always_free=True, description="查询料瓶、模具和漏斗占位情况")
    def material_status(self) -> dict[str, Any]:
        response = self._request("material_status", retry_read=True)
        data = response.get("data")
        if isinstance(data, dict):
            self._query_cache["material_status"] = (time.monotonic(), data)
            snapshot = empty_materials()
            for layer in snapshot:
                raw = data.get(layer) or []
                snapshot[layer] = [int(flag) for flag in list(raw)[:10]]
                snapshot[layer].extend([0] * (10 - len(snapshot[layer])))
            self._materials = snapshot
            self._sync_deck_occupancy(snapshot)
        return response

    @action(always_free=True, description="查询当前电导测试批次和执行进度")
    def batch_status(self) -> dict[str, Any]:
        return self._request("batch_status", retry_read=True)

    @action(always_free=True, description="按批次号查询样品测试结果；空则查当前批次")
    def batch_result(self, batch_id: str = "") -> dict[str, Any]:
        param = {"batch_id": str(batch_id).strip()}
        return self._request("batch_result", param, retry_read=True)

    @action(always_free=True, description="按月份查询批次号，空则查当月，格式 yyyy-MM")
    def query_batch(self, month: str = "") -> dict[str, Any]:
        month_text = str(month).strip()
        if month_text:
            try:
                datetime.strptime(month_text, "%Y-%m")
            except ValueError as exc:
                raise ValueError("month 必须是 yyyy-MM 格式") from exc
        return self._request("query_batch", {"month": month_text}, retry_read=True)

    @action(always_free=True, description="探测与电导工站的 TCP 连接")
    def test_connection(self) -> dict[str, Any]:
        try:
            response = self._request("station_status", retry_read=True)
        except ConductivityStationTransportError as exc:
            return {
                "connected": False,
                "ip": self.ip,
                "port": self.port,
                "error": str(exc),
            }
        return {
            "connected": True,
            "ip": self.ip,
            "port": self.port,
            "result": response.get("result"),
        }

    def _invalidate_batch_cache(self) -> None:
        self._query_cache.pop("batch_status", None)

    @action(description="启动整批自动实验，工站连续执行 1–13 步，无需再串联分步")
    def start_batch(self) -> dict[str, Any]:
        response = self._require_ok(self._request("start_batch"))
        self._invalidate_batch_cache()
        return response

    @action(description="当前样品完成后停止整批任务")
    def stop_current_batch(self) -> dict[str, Any]:
        response = self._require_ok(self._request("stop_current_batch"))
        self._invalidate_batch_cache()
        return response

    @not_action
    def manual_run(self, step: int) -> dict[str, Any]:
        """发送 TCP manual_run。整批实验请用 start_batch，不要串联 13 步。"""
        step_value = int(step)
        if not 1 <= step_value <= 13:
            raise ValueError("step 必须在 1 到 13 之间")
        response = self._require_ok(
            self._request("manual_run", {"step": step_value})
        )
        self._invalidate_batch_cache()
        return response

    @action(always_free=True, description="设置单个库位占位；layer=漏斗/烧结料瓶/模具，slot=A01")
    def set_slot_occupancy(
        self, layer: str, slot: str, occupied: bool = True
    ) -> dict[str, Any]:
        layer_key = resolve_layer(layer)
        index = parse_slot_index(slot)
        snapshot = empty_materials()
        snapshot.update({key: list(values) for key, values in self._materials.items()})
        snapshot[layer_key][index] = 1 if occupied else 0
        self._apply_occupancy(snapshot)
        return {
            "layer": layer_key,
            "slot": SLOT_LABELS[index],
            "occupied": bool(occupied),
            "materials": snapshot,
        }

    @action(always_free=True, description="写入演示 3 瓶 / 3 模 / 3 漏斗占位")
    def load_demo_materials(self) -> dict[str, Any]:
        return {"materials": self._apply_occupancy(demo_loaded_materials())}

    @action(description="手动单步：模具拆解（整批请用 start_batch）")
    def disassemble_mold(self) -> dict[str, Any]:
        return self.manual_run(1)

    @action(description="手动单步：模具测厚（整批请用 start_batch）")
    def measure_mold_thickness(self) -> dict[str, Any]:
        return self.manual_run(2)

    @action(description="手动单步：模具转移（整批请用 start_batch）")
    def transfer_mold(self) -> dict[str, Any]:
        return self.manual_run(3)

    @action(description="手动单步：漏斗转移（整批请用 start_batch）")
    def transfer_funnel(self) -> dict[str, Any]:
        return self.manual_run(4)

    @action(description="手动单步：烧结料瓶扫码与转移（整批请用 start_batch）")
    def scan_and_transfer_bottle(self) -> dict[str, Any]:
        return self.manual_run(5)

    @action(description="手动单步：加粉（整批请用 start_batch）")
    def add_powder(self) -> dict[str, Any]:
        return self.manual_run(6)

    @action(description="手动单步：烧结料瓶暂存（整批请用 start_batch）")
    def park_sintering_bottle(self) -> dict[str, Any]:
        return self.manual_run(7)

    @action(description="手动单步：模具组装（整批请用 start_batch）")
    def assemble_mold(self) -> dict[str, Any]:
        return self.manual_run(8)

    @action(description="手动单步：模具转移（组装后）（整批请用 start_batch）")
    def transfer_assembled_mold(self) -> dict[str, Any]:
        return self.manual_run(9)

    @action(description="手动单步：EIS测试（整批请用 start_batch）")
    def run_eis_test(self) -> dict[str, Any]:
        return self.manual_run(10)

    @action(description="手动单步：模具测厚（测试后）（整批请用 start_batch）")
    def measure_mold_thickness_after_eis(self) -> dict[str, Any]:
        return self.manual_run(11)

    @action(description="手动单步：电子电导率测试（整批请用 start_batch）")
    def run_electronic_conductivity_test(self) -> dict[str, Any]:
        return self.manual_run(12)

    @action(description="手动单步：测试物料转移至托盘（整批请用 start_batch）")
    def transfer_tested_material_to_tray(self) -> dict[str, Any]:
        return self.manual_run(13)
