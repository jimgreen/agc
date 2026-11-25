"""AGC 交直流与氢能协同控制决策算法核心包。

本包基于学员端新能源实时控制与拓扑优化算法独立封装，提供：
- 交直流微电网及氢能系统拓扑解析（ResourceTopology）
- 风光最大可发能力评估与储能 SOC 分段充放电降额
- 电制氢与燃料电池状态机协调控制
- 多孤岛 QP/LP 有功功率优化分配与保护带约束
- 策略计算入口 ``calculate_renewable_control_plan`` 与控制参数配置 ``RenewableControlSettings``
"""

from __future__ import annotations

from agc_engine.control_config import (
    DEFAULT_CONTROL_CONFIG,
    default_boolean,
    default_derating_curve,
    default_integer,
    default_number,
)
from agc_engine.control_parts.common import (
    DEFAULT_STORAGE_CHARGE_DERATING_CURVE,
    DEFAULT_STORAGE_DISCHARGE_DERATING_CURVE,
    MINIMUM_COMMAND_VALID_MINUTES,
    MINIMUM_CONTROL_INTERVAL_SECONDS,
    RENEWABLE_CONTROL_STRATEGY_ID,
)
from agc_engine.control_parts.planner import (
    DEVICE_CONTROL_STATE_LABELS,
    calculate_renewable_control_plan,
    evaluate_device_curve,
    resolve_snapshot_minute_of_day,
)
from agc_engine.control_parts.settings import (
    RenewableControlSettings,
)
from agc_engine.resource_topology import (
    ResourceTopology,
    ResourceTopologyStaticContext,
    prepare_resource_topology_static_context,
    resolve_resource_topology,
)

__all__ = [
    "DEFAULT_CONTROL_CONFIG",
    "DEFAULT_STORAGE_CHARGE_DERATING_CURVE",
    "DEFAULT_STORAGE_DISCHARGE_DERATING_CURVE",
    "MINIMUM_COMMAND_VALID_MINUTES",
    "MINIMUM_CONTROL_INTERVAL_SECONDS",
    "RENEWABLE_CONTROL_STRATEGY_ID",
    "RenewableControlSettings",
    "ResourceTopology",
    "ResourceTopologyStaticContext",
    "calculate_renewable_control_plan",
    "default_boolean",
    "default_derating_curve",
    "default_integer",
    "default_number",
    "prepare_resource_topology_static_context",
    "resolve_resource_topology",
]
