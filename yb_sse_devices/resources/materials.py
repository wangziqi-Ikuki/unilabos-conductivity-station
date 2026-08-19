"""电导工站占位耗材：漏斗、烧结料瓶、模具。"""

from __future__ import annotations

from typing import Any

try:
    from pylabrobot.resources import Container
except Exception:  # pragma: no cover
    class Container:  # type: ignore[no-redef]
        def __init__(self, name: str, size_x: float = 40.0, size_y: float = 40.0, size_z: float = 40.0, **kwargs: Any) -> None:
            self.name = name
            self._size_x = size_x
            self._size_y = size_y
            self._size_z = size_z
            self.kwargs = kwargs

try:
    from unilabos.registry.decorators import resource
except Exception:  # pragma: no cover
    def resource(*_args: Any, **_kwargs: Any):
        def decorator(cls):
            return cls

        return decorator


class _ConductivityPlaceholder(Container):
    resource_id = "conductivity_placeholder"

    def __init__(self, name: str, **kwargs: Any) -> None:
        # 与 warehouse 槽位同尺寸；过小前端只显示一个点（Bioyond placeholder size）。
        kwargs.setdefault("size_x", 70.0)
        kwargs.setdefault("size_y", 70.0)
        kwargs.setdefault("size_z", 40.0)
        kwargs.setdefault("category", "container")
        super().__init__(name, **kwargs)


@resource(
    id="conductivity_funnel",
    category=["labware", "container"],
    description="电导工站漏斗占位",
)
class ConductivityFunnel(_ConductivityPlaceholder):
    resource_id = "conductivity_funnel"


@resource(
    id="conductivity_sintering_bottle",
    category=["labware", "container"],
    description="电导工站烧结料瓶占位",
)
class ConductivitySinteringBottle(_ConductivityPlaceholder):
    resource_id = "conductivity_sintering_bottle"


@resource(
    id="conductivity_mold",
    category=["labware", "container"],
    description="电导工站模具占位",
)
class ConductivityMold(_ConductivityPlaceholder):
    resource_id = "conductivity_mold"


LAYER_RESOURCE_CLASSES = {
    "funnel": ConductivityFunnel,
    "bottle": ConductivitySinteringBottle,
    "mold": ConductivityMold,
}
