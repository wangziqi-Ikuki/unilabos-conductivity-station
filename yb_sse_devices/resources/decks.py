"""电导工站 Deck：漏斗 / 烧结料瓶 / 模具三层 2×5 料架。"""

from __future__ import annotations

from typing import Any

from yb_sse_devices.protocol import LAYER_WAREHOUSE_NAMES, RACK_LAYERS
from yb_sse_devices.resources.warehouses import LAYER_Y_PITCH, conductivity_rack_layer

try:
    from pylabrobot.resources import Coordinate, Deck
except Exception:  # pragma: no cover
    class Coordinate:  # type: ignore[no-redef]
        def __init__(self, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> None:
            self.x = x
            self.y = y
            self.z = z

    class Deck:  # type: ignore[no-redef]
        def __init__(self, name: str, size_x: float = 0, size_y: float = 0, size_z: float = 0, **kwargs: Any) -> None:
            self.name = name
            self.children: list[Any] = []
            self.kwargs = kwargs

        def assign_child_resource(self, resource: Any, location: Any = None) -> None:
            del location
            self.children.append(resource)

try:
    from unilabos.registry.decorators import resource
except Exception:  # pragma: no cover
    def resource(*_args: Any, **_kwargs: Any):
        def decorator(cls):
            return cls

        return decorator


@resource(
    id="ConductivityStation_Deck",
    category=["deck"],
    description="电导率自动化测试工站三层料架 Deck",
)
class ConductivityStation_Deck(Deck):
    def __init__(
        self,
        name: str = "ConductivityStation_Deck",
        size_x: float = 900.0,
        size_y: float = 900.0,
        size_z: float = 400.0,
        category: str = "deck",
        setup: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            name=name, size_x=size_x, size_y=size_y, size_z=size_z, category=category, **kwargs
        )
        self.warehouses: dict[str, Any] = {}
        self.warehouse_locations: dict[str, Any] = {}
        if setup:
            self.setup()

    def setup(self) -> None:
        if self.warehouses:
            return
        self.warehouses = {
            LAYER_WAREHOUSE_NAMES[layer]: conductivity_rack_layer(LAYER_WAREHOUSE_NAMES[layer])
            for layer in RACK_LAYERS
        }
        # 自上而下：模具 → 烧结料瓶 → 漏斗（与漏斗/模具对调）。
        self.warehouse_locations = {
            LAYER_WAREHOUSE_NAMES["mold"]: Coordinate(0.0, 0.0, 160.0),
            LAYER_WAREHOUSE_NAMES["bottle"]: Coordinate(0.0, LAYER_Y_PITCH, 80.0),
            LAYER_WAREHOUSE_NAMES["funnel"]: Coordinate(0.0, LAYER_Y_PITCH * 2, 0.0),
        }
        for layer in ("mold", "bottle", "funnel"):
            name = LAYER_WAREHOUSE_NAMES[layer]
            self.assign_child_resource(
                self.warehouses[name], location=self.warehouse_locations[name]
            )

    def get_site(self, warehouse_name: str, label: str) -> Any:
        """按 A01–B05 标签取一层料架上的库位。"""
        warehouse = self.warehouses[warehouse_name]
        return warehouse[label]
