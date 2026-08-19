"""电导工站三层料架：每层 2 行 × 5 列，库位 A01–B05。"""

from __future__ import annotations

from typing import Any

from yb_sse_devices.protocol import SLOT_LABELS

# 一层 2 行 × 80mm + 余量，供 Deck 纵向错开，避免三层叠在同一 x-y。
LAYER_Y_PITCH = 240.0


def conductivity_rack_layer(name: str) -> Any:
    """创建一层 2×5 堆栈料架。

    前端行优先展示为::

        A01 | A02 | A03 | A04 | A05
        B01 | B02 | B03 | B04 | B05
    """
    try:
        from unilabos.resources.warehouse import warehouse_factory
    except Exception:  # pragma: no cover
        return _FallbackWarehouse(name)

    return warehouse_factory(
        name=name,
        num_items_x=5,
        num_items_y=2,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=80.0,
        item_dy=80.0,
        item_dz=50.0,
        resource_size_x=70.0,
        resource_size_y=70.0,
        resource_size_z=40.0,
        category="warehouse",
        layout="row-major",
    )


class _FallbackWarehouse:
    """无 unilabos warehouse 时的 10 格占位，供单测占用刷新使用。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.sites: dict[str, Any] = {label: None for label in SLOT_LABELS}

    def __getitem__(self, key: str) -> Any:
        return self.sites[key]

    def __setitem__(self, key: str, resource: Any) -> None:
        self.sites[key] = resource
