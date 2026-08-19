"""电导工站协议常量：步骤名、料架槽位、设备字段。"""

from __future__ import annotations

from typing import Iterable, Sequence

STEP_NAMES: dict[int, str] = {
    1: "模具拆解",
    2: "模具测厚",
    3: "模具转移",
    4: "漏斗转移",
    5: "烧结料瓶扫码与转移",
    6: "加粉",
    7: "烧结料瓶暂存",
    8: "模具组装",
    9: "模具转移",
    10: "EIS测试",
    11: "模具测厚",
    12: "电子电导率测试",
    13: "测试物料转移至托盘",
}

# UniLab 动作名 → (step, 中文说明)。步骤 2/11、3/9 方法名必须区分。
STEP_ACTIONS: tuple[tuple[int, str, str], ...] = (
    (1, "disassemble_mold", "模具拆解"),
    (2, "measure_mold_thickness", "模具测厚"),
    (3, "transfer_mold", "模具转移"),
    (4, "transfer_funnel", "漏斗转移"),
    (5, "scan_and_transfer_bottle", "烧结料瓶扫码与转移"),
    (6, "add_powder", "加粉"),
    (7, "park_sintering_bottle", "烧结料瓶暂存"),
    (8, "assemble_mold", "模具组装"),
    (9, "transfer_assembled_mold", "模具转移（组装后）"),
    (10, "run_eis_test", "EIS测试"),
    (11, "measure_mold_thickness_after_eis", "模具测厚（测试后）"),
    (12, "run_electronic_conductivity_test", "电子电导率测试"),
    (13, "transfer_tested_material_to_tray", "测试物料转移至托盘"),
)

SLOT_COUNT = 10
SLOT_LABELS: tuple[str, ...] = (
    "A01",
    "A02",
    "A03",
    "A04",
    "A05",
    "B01",
    "B02",
    "B03",
    "B04",
    "B05",
)

# 合作方 HMI 使用 #1–#10；#1–#5 = A01–A05，#6–#10 = B01–B05。
SLOT_NUMBERS: tuple[int, ...] = tuple(range(1, SLOT_COUNT + 1))

DEVICE_KEYS: tuple[str, ...] = (
    "robot_arm",
    "scanner",
    "lid_open_close_mechanism",
    "powder_adding_mechanism",
    "tablet_pressing_mechanism",
    "electrochemical_workstation",
    "stack_rack",
)

DEVICE_LABELS: dict[str, str] = {
    "robot_arm": "机械臂",
    "scanner": "扫码器",
    "lid_open_close_mechanism": "开合盖机构",
    "powder_adding_mechanism": "加粉机构",
    "tablet_pressing_mechanism": "压片机构",
    "electrochemical_workstation": "电化学工作站",
    "stack_rack": "堆栈料架",
}

RACK_LAYERS: tuple[str, ...] = ("funnel", "bottle", "mold")

LAYER_WAREHOUSE_NAMES: dict[str, str] = {
    "funnel": "漏斗层",
    "bottle": "烧结料瓶层",
    "mold": "模具层",
}

LAYER_LABELS: dict[str, str] = {
    "funnel": "漏斗",
    "bottle": "烧结料瓶",
    "mold": "模具",
}

LAYER_ALIASES: dict[str, str] = {
    "funnel": "funnel",
    "漏斗": "funnel",
    "漏斗层": "funnel",
    "bottle": "bottle",
    "烧结料瓶": "bottle",
    "烧结料瓶层": "bottle",
    "mold": "mold",
    "模具": "mold",
    "模具层": "mold",
}

HEALTH_LABELS: dict[str, str] = {
    "IDLE": "空闲",
    "BUSY": "运行中",
    "FAULT": "故障",
    "OFFLINE": "离线",
}


def resolve_layer(name: str) -> str:
    key = str(name).strip()
    layer = LAYER_ALIASES.get(key) or LAYER_ALIASES.get(key.lower())
    if not layer:
        raise ValueError(f"未知料架层: {name}")
    return layer


def parse_slot_index(slot: str | int) -> int:
    """A01–B05、#1–#10 或 0–9 下标 → 0-based。"""
    text = str(slot).strip()
    if text.isdigit() or (text.startswith("#") and text[1:].isdigit()):
        number = int(text[1:] if text.startswith("#") else text)
        if 1 <= number <= SLOT_COUNT:
            return number - 1
        if 0 <= number < SLOT_COUNT:
            return number
    label = text.upper()
    if label in SLOT_LABELS:
        return SLOT_LABELS.index(label)
    raise ValueError("slot 必须是 A01–B05 或 1–10")


def slot_label(index: int) -> str:
    """协议 0-based 下标 → A01–B05。"""
    if not 0 <= index < SLOT_COUNT:
        raise IndexError(f"槽位下标必须在 0 到 {SLOT_COUNT - 1} 之间")
    return SLOT_LABELS[index]


def slot_number(index: int) -> int:
    """协议 0-based 下标 → HMI #1–#10。"""
    if not 0 <= index < SLOT_COUNT:
        raise IndexError(f"槽位下标必须在 0 到 {SLOT_COUNT - 1} 之间")
    return SLOT_NUMBERS[index]


def occupied_indices(values: Sequence[int] | None) -> list[int]:
    """返回占用为 1 的 0-based 下标。"""
    if not values:
        return []
    return [index for index, occupied in enumerate(values[:SLOT_COUNT]) if int(occupied)]


def occupied_slot_labels(values: Sequence[int] | None) -> list[str]:
    """占用槽位的 A01–B05 标签。"""
    return [SLOT_LABELS[index] for index in occupied_indices(values)]


def occupied_count(values: Sequence[int] | None) -> int:
    return len(occupied_indices(values))


def occupancy_list(occupied: Iterable[int] | None = None) -> list[int]:
    """生成长度 10 的 0/1 占用数组；occupied 为 0-based 下标。"""
    flags = [0] * SLOT_COUNT
    for index in occupied or ():
        flags[int(index)] = 1
    return flags


def empty_materials() -> dict[str, list[int]]:
    return {layer: [0] * SLOT_COUNT for layer in RACK_LAYERS}


def demo_loaded_materials() -> dict[str, list[int]]:
    """测试用：瓶 1–3、模 4–6、漏斗 7–9 在位，数量对齐可 start_batch。"""
    return {
        "bottle": occupancy_list((0, 1, 2)),
        "mold": occupancy_list((3, 4, 5)),
        "funnel": occupancy_list((6, 7, 8)),
    }


def online_devices(online: bool = True) -> dict[str, int]:
    flag = 1 if online else 0
    return {key: flag for key in DEVICE_KEYS}


RESULT_MESSAGES: dict[int, str] = {
    0: "成功",
    1: "工站离线或 PLC 未就绪",
    2: "库位为空或瓶/模/漏斗数量不一致，请先在 Mock 页备料",
    3: "没有正在运行的批次，请先 start_batch",
    4: "已有批次在运行",
    5: "步骤号无效",
    6: "工站忙，请稍后再发分步动作",
    7: "请求解析失败",
    8: "未知动作",
    9: "找不到该批次",
}


def protocol_error_message(result: int) -> str:
    code = int(result)
    text = RESULT_MESSAGES.get(code, "工站返回错误")
    return f"{text} (result={code})"


def summarize_station_health(
    devices: dict[str, int] | None,
    *,
    connected: bool,
    batch_running: bool = False,
) -> str:
    """看板可识别：IDLE / BUSY / FAULT / OFFLINE。"""
    if not connected:
        return "OFFLINE"
    values = [int(devices.get(key, 0)) for key in DEVICE_KEYS] if devices else []
    if not values or all(value == 0 for value in values):
        return "OFFLINE"
    if not all(value == 1 for value in values):
        return "FAULT"
    if batch_running:
        return "BUSY"
    return "IDLE"
