"""电导工站 TCP 客户端与模拟服务端集成测试。"""

from __future__ import annotations

import json
import math
import socketserver
import threading
import time
from datetime import datetime

import pytest

from yb_sse_devices.conductivity import (
    ConductivityStation,
    ConductivityStationProtocolError,
    ConductivityStationTransportError,
)
from yb_sse_devices.mock_server import (
    MockConductivityServer,
    MockConductivityState,
    start_mock_in_process,
)
from yb_sse_devices.protocol import SLOT_LABELS, demo_loaded_materials, empty_materials
from yb_sse_devices.resources.decks import ConductivityStation_Deck


@pytest.fixture()
def mock_state() -> MockConductivityState:
    return MockConductivityState(
        step_interval=0.01, materials=demo_loaded_materials()
    )


@pytest.fixture()
def station(mock_state: MockConductivityState) -> ConductivityStation:
    server = MockConductivityServer(("127.0.0.1", 0), state=mock_state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = ConductivityStation(
        ip="127.0.0.1",
        port=server.server_address[1],
        response_timeout=1.0,
    )
    try:
        yield client
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_status_material_start_and_result(station: ConductivityStation) -> None:
    assert station.station_status()["data"]["robot_arm"] == 1
    assert station.material_status()["data"]["bottle"][:3] == [1, 1, 1]
    assert station.status == "IDLE"
    assert station.station_health == "空闲"
    assert station.connected is True
    assert station.funnel_remaining == 3
    assert station.pending_bottles == 3
    assert station.pending_molds == 3

    started = station.start_batch()
    assert started["result"] == 0
    batch_id = started["data"]["batch_id"]
    assert station.batch_running is True

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        status = station.batch_status()["data"]
        if not status["running"]:
            break
        time.sleep(0.02)
    else:
        pytest.fail("模拟批次未在预期时间内完成")
    assert status["batch"]["current_test"] == 3
    assert status["batch"]["current_test_step"] == 13
    assert station.current_test_step == 13

    result = station.batch_result(batch_id)
    tests = result["data"]["batch"]["tests"]
    assert [item["state"] for item in tests] == ["finished"] * 3
    assert tests[0]["bottle_code"].startswith("LOT-")
    assert all(item["test_time"] for item in tests)
    assert tests[0]["R"] > 0
    assert tests[0]["ion_conductivity"] > 0
    assert tests[0]["area"] == pytest.approx(math.pi * 0.5**2)
    thickness_cm = (tests[0]["h2"] - tests[0]["h1"]) / 10.0
    expected_ms_cm = thickness_cm / (tests[0]["R"] * tests[0]["area"]) * 1000
    assert tests[0]["ion_conductivity"] == pytest.approx(expected_ms_cm, rel=1e-5)


def test_named_step_action_sends_manual_run(station: ConductivityStation) -> None:
    station.start_batch()
    assert station.add_powder()["result"] == 0


def test_set_slot_occupancy_writes_to_mock(
    station: ConductivityStation,
    mock_state: MockConductivityState,
) -> None:
    mock_state.clear_materials()
    station._query_cache.clear()
    result = station.set_slot_occupancy("模具层", "A01", True)
    assert result["slot"] == "A01"
    assert mock_state.materials["mold"][0] == 1
    station._query_cache.clear()
    assert station.pending_molds == 1


def test_manual_validation_and_stop_after_current_sample(
    station: ConductivityStation,
) -> None:
    with pytest.raises(ValueError, match="1 到 13"):
        station.manual_run(14)

    batch_id = station.start_batch()["data"]["batch_id"]
    assert station.manual_run(6)["result"] == 0
    assert station.stop_current_batch()["result"] == 0

    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        status = station.batch_status()["data"]
        if not status["running"]:
            break
        time.sleep(0.02)
    assert status["batch"]["state"] == "stopped"
    tests = station.batch_result(batch_id)["data"]["batch"]["tests"]
    assert tests[0]["state"] == "finished"
    assert tests[1]["state"] == "not_started"


def test_unknown_batch_empty_id_and_query(
    station: ConductivityStation,
    mock_state: MockConductivityState,
) -> None:
    assert station.batch_result("missing")["result"] == 9
    assert station.batch_result("")["result"] == 9
    batch_id = station.start_batch()["data"]["batch_id"]
    current = station.batch_result("")
    assert current["result"] == 0
    assert current["data"]["batch"]["batch_id"] == batch_id
    month = datetime.now().strftime("%Y-%m")
    queried = station.query_batch(month)
    assert batch_id in queried["data"]["batch_ids"]
    assert station.query_batch("")["data"]["batch_ids"]
    with mock_state.lock:
        mock_state.batch["running"] = False
        mock_state.batch["state"] = "completed"
    # TCP clear 仍可用，但 UniLab 不再封装该动作。
    assert mock_state.handle({"request_id": 99, "action": "clear_current_batch"})["result"] == 0
    status = station.batch_status()["data"]
    assert status == {"running": False, "batch": None}


def test_device_offline_summary_and_material_edit(
    station: ConductivityStation,
    mock_state: MockConductivityState,
) -> None:
    mock_state.set_device("electrochemical_workstation", False)
    station._query_cache.clear()
    assert station.electrochemical_workstation == 0
    assert station.status == "FAULT"
    mock_state.set_device("robot_arm", False)
    mock_state.set_device("scanner", False)
    mock_state.set_device("lid_open_close_mechanism", False)
    mock_state.set_device("powder_adding_mechanism", False)
    mock_state.set_device("tablet_pressing_mechanism", False)
    mock_state.set_device("stack_rack", False)
    station._query_cache.clear()
    assert station.status == "OFFLINE"
    with pytest.raises(ConductivityStationProtocolError, match="result=1"):
        station.start_batch()


def test_occupancy_syncs_to_deck(
    station: ConductivityStation,
    mock_state: MockConductivityState,
) -> None:
    deck = ConductivityStation_Deck(setup=True)
    station.attach_deck(deck)
    station.material_status()
    site = deck.get_site("烧结料瓶层", "A01")
    assert site is not None
    assert type(site).__name__ != "ResourceHolder"
    mock_state.set_slot("bottle", 0, False)
    station._query_cache.clear()
    station.material_status()
    site = deck.get_site("烧结料瓶层", "A01")
    assert site is None or type(site).__name__ == "ResourceHolder"
    assert station.pending_bottles == 2


def test_manual_run_busy_returns_6(mock_state: MockConductivityState) -> None:
    mock_state.step_interval = 2.0
    server = MockConductivityServer(("127.0.0.1", 0), state=mock_state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = ConductivityStation(
        ip="127.0.0.1",
        port=server.server_address[1],
        response_timeout=1.0,
    )
    try:
        client.start_batch()
        assert client.manual_run(2)["result"] == 0
        with pytest.raises(ConductivityStationProtocolError, match="result=6"):
            client.manual_run(3)
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_use_mock_starts_in_process_server() -> None:
    client = ConductivityStation(ip="127.0.0.1", port=0, use_mock=True, response_timeout=1.0)
    try:
        assert client.port != 0
        assert client.test_connection()["connected"] is True
        assert client.station_status()["result"] == 0
    finally:
        client.close()


def test_interactive_ui_can_edit_slots() -> None:
    urllib_request = pytest.importorskip("urllib.request")
    runtime = start_mock_in_process(
        "127.0.0.1",
        0,
        state=MockConductivityState(materials=empty_materials()),
        enable_ui=True,
        ui_port=0,
    )
    try:
        payload = json.dumps(
            {"layer": "funnel", "index": 0, "occupied": True}
        ).encode("utf-8")
        request = urllib_request.Request(
            f"http://127.0.0.1:{runtime.ui_port}/api/slot",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib_request.urlopen(request, timeout=2) as response:
            body = json.loads(response.read().decode("utf-8"))
        assert body["materials"]["funnel"][0] == 1
        client = ConductivityStation(
            ip="127.0.0.1", port=runtime.port, response_timeout=1.0
        )
        try:
            assert client.funnel_remaining == 1
            payload_state = runtime.status_payload()
            assert payload_state["last_client"]
            assert payload_state["last_action"]["action"] == "material_status"
        finally:
            client.close()
    finally:
        runtime.close()


def _scripted_station(plans: list[object]) -> tuple[ConductivityStation, socketserver.TCPServer, threading.Thread]:
    """启动一个按连接顺序返回分片、断线或指定响应的协议测试服务。"""

    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            received = bytearray()
            while b"\r\n" not in received:
                chunk = self.request.recv(4096)
                if not chunk:
                    return
                received.extend(chunk)
            request = json.loads(bytes(received).split(b"\r\n", 1)[0])
            with self.server.plan_lock:  # type: ignore[attr-defined]
                plan = self.server.plans.pop(0)  # type: ignore[attr-defined]
            if plan is None:
                return
            if callable(plan):
                plan = plan(request)
            chunks = plan if isinstance(plan, list) else [plan]
            for chunk in chunks:
                self.request.sendall(chunk)

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.plans = list(plans)  # type: ignore[attr-defined]
    server.plan_lock = threading.Lock()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = ConductivityStation(
        ip="127.0.0.1", port=server.server_address[1], response_timeout=0.5
    )
    return client, server, thread


def _close_scripted_station(
    client: ConductivityStation,
    server: socketserver.TCPServer,
    thread: threading.Thread,
) -> None:
    client.close()
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_tcp_fragmentation_and_read_retry() -> None:
    def fragmented(request: dict) -> list[bytes]:
        payload = json.dumps(
            {"request_id": request["request_id"], "result": 0, "data": {"running": False, "batch": None}}
        ).encode()
        return [payload[:8], payload[8:] + b"\r\n"]

    client, server, thread = _scripted_station([None, fragmented])
    try:
        # 查询动作首次断线后可安全重连，且能拼接分片响应。
        assert client.batch_status()["data"]["running"] is False
        assert server.plans == []  # type: ignore[attr-defined]
    finally:
        _close_scripted_station(client, server, thread)

@pytest.mark.parametrize(
    "response,exception",
    [
        (b'{"request_id":999,"result":0}\r\n', ConductivityStationProtocolError),
        (b'{not-json}\r\n', ConductivityStationProtocolError),
    ],
)
def test_invalid_responses_are_rejected(response: bytes, exception: type[Exception]) -> None:
    client, server, thread = _scripted_station([response])
    try:
        with pytest.raises(exception):
            client.start_batch()
    finally:
        _close_scripted_station(client, server, thread)


def test_side_effect_action_is_not_retried_after_disconnect() -> None:
    client, server, thread = _scripted_station([None, b'{"request_id":1,"result":0}\r\n'])
    try:
        with pytest.raises(ConductivityStationTransportError):
            client.start_batch()
        # 第二个计划仍在，证明有副作用动作未被自动重发。
        assert len(server.plans) == 1  # type: ignore[attr-defined]
    finally:
        _close_scripted_station(client, server, thread)


def test_rack_slot_labels() -> None:
    assert SLOT_LABELS[0] == "A01"
    assert SLOT_LABELS[5] == "B01"
    assert SLOT_LABELS[9] == "B05"


def test_empty_materials_start_batch_raises(
    station: ConductivityStation,
    mock_state: MockConductivityState,
) -> None:
    mock_state.clear_materials()
    station._query_cache.clear()
    with pytest.raises(ConductivityStationProtocolError, match="result=2"):
        station.start_batch()
    with pytest.raises(ConductivityStationProtocolError, match="result=3"):
        station.disassemble_mold()


def test_station_creates_deck_with_sites() -> None:
    client = ConductivityStation(ip="127.0.0.1", port=1, response_timeout=0.1)
    try:
        assert client.deck is not None
        assert "漏斗层" in client.deck.warehouses
        assert client.deck.get_site("漏斗层", "A01") is not None
        assert client.deck.get_site("烧结料瓶层", "B05") is not None
    finally:
        client.close()


def test_mock_ui_exposes_step_snapshot(mock_state: MockConductivityState) -> None:
    mock_state.handle({"request_id": 1, "action": "start_batch"})
    snap = mock_state.snapshot()
    assert snap["running"] is True
    assert snap["current_test_step"] >= 1
    assert snap["last_action"]["action"] == "start_batch"
    assert snap["last_action"]["result"] == 0


def test_manual_mode_keeps_batch_for_named_steps() -> None:
    state = MockConductivityState(
        step_interval=0.01,
        materials=demo_loaded_materials(),
        auto_advance=False,
    )
    server = MockConductivityServer(("127.0.0.1", 0), state=state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = ConductivityStation(
        ip="127.0.0.1",
        port=server.server_address[1],
        response_timeout=1.0,
    )
    try:
        assert client.start_batch()["result"] == 0
        time.sleep(0.05)
        assert client.batch_running is True
        assert client.disassemble_mold()["result"] == 0
        assert client.current_test_step == 1
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_mock_reset_clears_batch_and_slots() -> None:
    urllib_request = pytest.importorskip("urllib.request")
    runtime = start_mock_in_process(
        "127.0.0.1",
        0,
        state=MockConductivityState(
            materials=demo_loaded_materials(),
            auto_advance=False,
        ),
        enable_ui=True,
        ui_port=0,
    )
    try:
        runtime.state.handle({"request_id": 1, "action": "start_batch"})
        request = urllib_request.Request(
            f"http://127.0.0.1:{runtime.ui_port}/api/reset",
            method="POST",
        )
        with urllib_request.urlopen(request, timeout=2) as response:
            body = json.loads(response.read().decode("utf-8"))
        assert body["materials"]["mold"] == [0] * 10
        assert body["running"] is False
        assert body["batch_id"] is None
        assert all(body["devices"].values())
    finally:
        runtime.close()


def test_deck_layers_are_separated_on_y() -> None:
    deck = ConductivityStation_Deck(setup=True)
    funnel_y = deck.warehouse_locations["漏斗层"].y
    bottle_y = deck.warehouse_locations["烧结料瓶层"].y
    mold_y = deck.warehouse_locations["模具层"].y
    assert bottle_y > mold_y
    assert funnel_y > bottle_y


def test_occupancy_poll_refreshes_materials(
    mock_state: MockConductivityState,
) -> None:
    server = MockConductivityServer(("127.0.0.1", 0), state=mock_state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = ConductivityStation(
        ip="127.0.0.1",
        port=server.server_address[1],
        response_timeout=1.0,
        occupancy_poll_interval=0.05,
    )

    class _Tracker:
        def add_resource(self, resource: object) -> None:
            del resource

    class _Node:
        resource_tracker = _Tracker()

    try:
        client.post_init(_Node())
        mock_state.clear_materials()
        mock_state.set_slot("mold", 0, True)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if client._materials.get("mold", [0])[0] == 1:
                break
            time.sleep(0.02)
        else:
            pytest.fail("占用轮询未刷新模具库位")
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
