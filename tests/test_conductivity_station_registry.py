from __future__ import annotations

import ast
from pathlib import Path

from unilabos.registry.decorators import (
    get_action_meta,
    get_device_meta,
    get_topic_config,
    is_not_action,
)

from yb_sse_devices.protocol import STEP_ACTIONS


ROOT = Path(__file__).resolve().parents[1]
DEVICE_DIR = ROOT / "yb_sse_devices"

STEP_ACTION_NAMES = {name for _step, name, _label in STEP_ACTIONS}


def test_conductivity_station_actions_are_discoverable_by_registry() -> None:
    tree = ast.parse(
        (DEVICE_DIR / "conductivity.py").read_text(encoding="utf-8")
    )
    device_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ConductivityStation"
    )
    device_decorator = next(
        decorator
        for decorator in device_class.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Name)
        and decorator.func.id == "device"
    )
    device_id = next(
        keyword.value.value
        for keyword in device_decorator.keywords
        if keyword.arg == "id" and isinstance(keyword.value, ast.Constant)
    )
    actions = {
        method.name
        for method in device_class.body
        if isinstance(method, ast.FunctionDef)
        and any(
            (isinstance(decorator, ast.Name) and decorator.id == "action")
            or (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Name)
                and decorator.func.id == "action"
            )
            for decorator in method.decorator_list
        )
    }
    init_method = next(
        method
        for method in device_class.body
        if isinstance(method, ast.FunctionDef) and method.name == "__init__"
    )

    assert device_id == "conductivity_station"
    assert actions == {
        "station_status",
        "material_status",
        "batch_status",
        "batch_result",
        "query_batch",
        "test_connection",
        "start_batch",
        "stop_current_batch",
        "set_slot_occupancy",
        "load_demo_materials",
        *STEP_ACTION_NAMES,
    }
    assert "clear_current_batch" not in actions
    assert "manual_run" not in actions
    assert {arg.arg for arg in init_method.args.args if arg.arg != "self"} == {
        "device_id",
        "config",
        "ip",
        "port",
        "connect_timeout",
        "response_timeout",
        "max_message_bytes",
        "encoding",
        "frame_delimiter",
        "station_action_names",
        "use_mock",
        "occupancy_poll_interval",
        "load_demo_occupancy",
        "occupancy",
    }

    close_method = next(
        method
        for method in device_class.body
        if isinstance(method, ast.FunctionDef) and method.name == "close"
    )
    assert any(
        isinstance(decorator, ast.Name) and decorator.id == "not_action"
        for decorator in close_method.decorator_list
    )

    status_method = next(
        method
        for method in device_class.body
        if isinstance(method, ast.FunctionDef) and method.name == "status"
    )
    assert any(
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Name)
        and decorator.func.id == "topic_config"
        for decorator in status_method.decorator_list
    )


def test_template_style_config_is_supported() -> None:
    from yb_sse_devices import ConductivityStation

    station = ConductivityStation(
        device_id="CONDUCTIVITY_STATION_1",
        config={
            "ip": "192.0.2.10",
            "port": 20001,
            "response_timeout": 30,
            "station_action_names": {"station_status": "status"},
        },
    )

    assert station.device_id == "CONDUCTIVITY_STATION_1"
    assert station.ip == "192.0.2.10"
    assert station.port == 20001
    assert station.response_timeout == 30
    assert station.station_action_names == {"station_status": "status"}
    assert station.status == "OFFLINE"


def test_status_and_batch_properties_are_topics() -> None:
    from yb_sse_devices import ConductivityStation

    topic_names = (
        "status",
        "station_health",
        "connected",
        "robot_arm",
        "scanner",
        "lid_open_close_mechanism",
        "powder_adding_mechanism",
        "tablet_pressing_mechanism",
        "electrochemical_workstation",
        "stack_rack",
        "funnel_remaining",
        "pending_bottles",
        "pending_molds",
        "batch_running",
        "current_test_step",
        "finished_count",
        "failed_count",
    )
    for name in topic_names:
        attr = getattr(ConductivityStation, name)
        assert get_topic_config(attr.fget) != {}


def test_template_decorator_metadata_is_registered() -> None:
    from yb_sse_devices import ConductivityStation

    device_meta = get_device_meta(ConductivityStation)

    assert device_meta is not None
    assert device_meta["device_id"] == "conductivity_station"
    assert device_meta["displayname"] == "电导率自动化测试工站"
    assert get_action_meta(ConductivityStation.station_status)["always_free"] is True
    assert get_topic_config(ConductivityStation.status.fget) != {}
    assert is_not_action(ConductivityStation.close)
    assert is_not_action(ConductivityStation.manual_run)


def test_reserved_device_directories_do_not_register_placeholders() -> None:
    synthesis_module = DEVICE_DIR / "synthesis_station.py"
    reserved_directories = [
        DEVICE_DIR / "characterization" / "xrd",
        DEVICE_DIR / "characterization" / "raman",
    ]

    assert synthesis_module.is_file()
    synthesis_tree = ast.parse(synthesis_module.read_text(encoding="utf-8"))
    assert not any(
        isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        for node in synthesis_tree.body
    )

    for directory in reserved_directories:
        assert directory.is_dir()
        assert (directory / "README.md").is_file()
        init_tree = ast.parse((directory / "__init__.py").read_text(encoding="utf-8"))
        assert not any(
            isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            for node in init_tree.body
        )


def test_mock_unilab_module_is_removed() -> None:
    assert not (DEVICE_DIR / "mock_unilab.py").exists()


def test_resource_modules_import_without_deadlock() -> None:
    import importlib
    from concurrent.futures import ThreadPoolExecutor, wait

    names = (
        "yb_sse_devices.resources.decks",
        "yb_sse_devices.resources.materials",
        "yb_sse_devices.conductivity",
        "yb_sse_devices",
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(importlib.import_module, name) for name in names]
        wait(futures)
        for future in futures:
            future.result()
