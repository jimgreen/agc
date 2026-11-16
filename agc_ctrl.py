import argparse
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple


ROOT_DIR = Path(__file__).resolve().parents[1]
AGC_DIR = Path(__file__).resolve().parent
for path in (ROOT_DIR, AGC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from efile_read import EBook, efile_factory

try:
    from agc_db import AGCDataStore, SQLiteLogHandler
except ImportError:  # pragma: no cover - keeps legacy standalone deployments usable
    AGCDataStore = None
    SQLiteLogHandler = None


DEFAULT_MODEL_FILE = AGC_DIR / "model_real.e"
DEFAULT_LOG_DIR = ROOT_DIR / "log"
DEFAULT_CTRL_OUTPUT_FILE = ROOT_DIR / "dev_ctrl.e"
DEFAULT_CTRL_TRIGGER = 60
DEFAULT_GATHER_TRIGGER = 10
DEFAULT_BALANCE_DEADBAND = 1e-3
DEFAULT_DIESEL_MIN_DEADBAND = 1e-3
DEFAULT_STORAGE_SOC_DEADBAND = 0.0
# Legacy direct dispatch helpers retain their historical unbounded setting;
# the default measured AGC path is bounded by DEFAULT_AGC_PROBE_STEP below.
DEFAULT_CONTROL_STEP = 1e9
DEFAULT_CONTROL_DEADBAND = 0.0
# The default controller deliberately uses a small probe per cycle.  A plant
# can override this with the AGC_PROBE_STEP parameter or a device p_step.
DEFAULT_AGC_PROBE_STEP = 5.0
# Unless explicitly configured, the SOC energy limit is evaluated over one
# control period.  This keeps the acceptance-space probe aligned with the
# small-step controller rather than imposing an arbitrary one-hour window.
DEFAULT_ACCEPTANCE_HORIZON_H = 0.0
YT_ROLE_HYDROGEN_ABSORB = "HYDROGEN_ABSORB"
YT_ROLE_FUEL_CELL_SUPPORT = "FUEL_CELL_SUPPORT"

_RESOURCE_RENEWABLE_TOKENS = (
    "wind", "solar", "photovoltaic", "pv", "renew", "green",
    "风", "光伏", "光电", "风电", "新能源", "可再生",
)
_RESOURCE_STORAGE_TOKENS = (
    "storage", "battery", "bess", "ess", "储能", "电池",
)
_RESOURCE_THERMAL_TOKENS = (
    "diesel", "thermal", "火电", "柴", "柴油",
    "燃机", "coal", "gas_turbine", "conventional", "常规",
)


@dataclass(frozen=True)
class FesClient:
    get_yc_value: Callable
    get_yx_value: Callable
    schema: object = None
    context: object = None


class SystemConfig:
    """Runtime state for the AGC loop."""

    def __init__(
        self,
        model,
        ctrl_trigger: int = DEFAULT_CTRL_TRIGGER,
        gather_trigger: int = DEFAULT_GATHER_TRIGGER,
        log_file: Optional[Path] = None,
        ctrl_output_file: Optional[Path] = DEFAULT_CTRL_OUTPUT_FILE,
        fes_client: Optional[FesClient] = None,
        data_store=None,
    ):
        self.model = model
        self.ctrl_trigger = max(1, int(ctrl_trigger))
        self.gather_trigger = max(1, int(gather_trigger))
        self.fes_client = fes_client or _load_fes_client()
        self.is_running = True
        self.control_count = 0
        self.gather_count = 0
        self.ctrl_output_file = None if ctrl_output_file is None else Path(ctrl_output_file)
        self.data_store = data_store
        self.logger = self._setup_logger(log_file or _default_log_file())

    @staticmethod
    def _setup_logger(log_file: Path) -> logging.Logger:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger("AGCControlSystem")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.handlers.clear()

        formatter = logging.Formatter(
            "%(asctime)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)
        return logger

    def send_yt_cmd(self, yt, value: float) -> None:
        yt.ctrl_value = float(value)
        self.logger.info("下发遥调指令 %s rtu=%s pnt=%s value=%s", yt.name, yt.rtu, yt.pnt, value)


def _default_log_file() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return DEFAULT_LOG_DIR / f"control_system_{timestamp}.log"


def _load_fes_client() -> FesClient:
    try:
        from fes_interface import get_yc_value, get_yx_value
    except ImportError:
        return FesClient(
            get_yc_value=lambda schema, context, rtu, pnt: (False, "", None),
            get_yx_value=lambda schema, context, rtu, pnt: (False, "", None),
        )

    try:
        from sys_para import CONTEXT, SCHEMA
    except ImportError:
        SCHEMA = None
        CONTEXT = None
    return FesClient(get_yc_value=get_yc_value, get_yx_value=get_yx_value, schema=SCHEMA, context=CONTEXT)


def _load_control_functions() -> Tuple[Callable, Callable, Callable]:
    try:
        from do_one_ctrl import agc_ctrl_stg_check, do_one_ctrl, do_renew_gen_fur_calc

        return do_one_ctrl, agc_ctrl_stg_check, do_renew_gen_fur_calc
    except ImportError:
        def do_one_ctrl(logger, model):
            do_wind_solar_priority_ctrl(logger, model)

        def agc_ctrl_stg_check(logger, model):
            agc_ctrl_stg_check_default(logger, model)

        def do_renew_gen_fur_calc(logger, model, wind_speed, solar_irrad, env_temp):
            do_renew_gen_fur_calc_default(logger, model, wind_speed, solar_irrad, env_temp)

        return do_one_ctrl, agc_ctrl_stg_check, do_renew_gen_fur_calc


def _object_id(item) -> str:
    return str(getattr(item, "id", ""))


def _build_id_map(items: Iterable) -> Dict[str, object]:
    return {_object_id(item): item for item in items}


def _find_by_id(mapping: Dict[str, object], item_id, logger: logging.Logger, label: str):
    item = mapping.get(str(item_id))
    if item is None:
        logger.info("not find %s %s", label, item_id)
    return item


def get_para(model, name: str, default, cast):
    if not hasattr(model, "_para_by_name"):
        model._para_by_name = {str(para.name): para for para in getattr(model, "para", [])}
    para = model._para_by_name.get(name)
    if para is None:
        return default
    try:
        return cast(para.value)
    except (TypeError, ValueError):
        return default


def get_para_int(model, name: str, default: int) -> int:
    return get_para(model, name, default, int)


def get_para_float(model, name: str, default: float) -> float:
    return get_para(model, name, default, float)


def get_para_str(model, name: str, default: str) -> str:
    return get_para(model, name, default, str)


def initialize_system(
    emodel,
    log_file: Optional[Path] = None,
    ctrl_output_file: Optional[Path] = DEFAULT_CTRL_OUTPUT_FILE,
    ctrl_trigger: Optional[int] = None,
    gather_trigger: Optional[int] = None,
) -> SystemConfig:
    """Initialize device links and runtime config."""
    data_store = None
    if AGCDataStore is not None:
        try:
            data_store = AGCDataStore()
            data_store.apply_model(emodel)
            data_store.apply_latest_runtime(emodel)
            data_store.apply_latest_environment(emodel)
        except Exception:
            if data_store is not None:
                try:
                    data_store.close()
                except Exception:
                    pass
            data_store = None
    ctrl = ctrl_trigger if ctrl_trigger is not None else get_para_int(emodel, "CTRL_TRIGGER", DEFAULT_CTRL_TRIGGER)
    gather = gather_trigger if gather_trigger is not None else get_para_int(emodel, "GATHER_TRIGGER", DEFAULT_GATHER_TRIGGER)
    config = SystemConfig(
        emodel,
        ctrl_trigger=ctrl,
        gather_trigger=gather,
        log_file=log_file,
        ctrl_output_file=ctrl_output_file,
        data_store=data_store,
    )
    if data_store is not None and SQLiteLogHandler is not None:
        db_log_handler = SQLiteLogHandler(data_store)
        db_log_handler.setFormatter(logging.Formatter("%(message)s"))
        config.logger.addHandler(db_log_handler)
        config.db_log_handler = db_log_handler
    if data_store is not None:
        config.control_count = data_store.latest_cycle()
    emodel.config = config
    emodel.ctrl_trigger = config.ctrl_trigger
    emodel.gather_trigger = config.gather_trigger
    yc_by_id = _build_id_map(getattr(emodel, "yc", []))
    yx_by_id = _build_id_map(getattr(emodel, "yx", []))
    emodel.yc_by_id = yc_by_id
    emodel.yx_by_id = yx_by_id

    emodel.renew_generator = list(getattr(emodel, "wind_generator", [])) + list(getattr(emodel, "pv_generator", []))
    for gen in getattr(emodel, "diesel_generator", []):
        gen.p_yc = _find_by_id(yc_by_id, gen.p_yc_id, config.logger, "柴油发电机遥测")
    for gen in emodel.renew_generator:
        gen.p_yc = _find_by_id(yc_by_id, gen.p_yc_id, config.logger, "新能源遥测")
    for load in getattr(emodel, "energyconsumer", []):
        load.p_yc = _find_by_id(yc_by_id, load.p_yc_id, config.logger, "负荷遥测")
    for estore in getattr(emodel, "estorage", []):
        estore.p_yc = _find_by_id(yc_by_id, estore.p_yc_id, config.logger, "储能功率遥测")
        estore.soc_yc = _find_by_id(yc_by_id, estore.soc_yc_id, config.logger, "储能 SOC 遥测")

    _prepare_agc_resources(emodel, config.logger, yc_by_id)

    env_by_name = {str(env.name): env for env in getattr(emodel, "env_para", [])}
    emodel.wind_speed = _find_by_id(yc_by_id, getattr(env_by_name.get("wind_speed"), "yc_id", None), config.logger, "风速遥测")
    emodel.solor_irrad = _find_by_id(yc_by_id, getattr(env_by_name.get("solor_irrad"), "yc_id", None), config.logger, "辐照遥测")
    emodel.env_temp = _find_by_id(yc_by_id, getattr(env_by_name.get("env_temp"), "yc_id", None), config.logger, "气温遥测")
    return config


def _resource_text(device) -> str:
    """Return explicit resource metadata and the device name as one token string."""

    values = [
        getattr(device, "agc_role", ""),
        getattr(device, "resource_type", ""),
        getattr(device, "technology", ""),
        getattr(device, "fuel_type", ""),
        getattr(device, "dev_type", ""),
        getattr(device, "name", ""),
        type(device).__name__,
    ]
    return " ".join(str(value).strip().lower() for value in values if value not in (None, ""))


def _classify_resource(device, *, side: str, fallback: str) -> str:
    """Classify an AC/DC device without changing its model type.

    Explicit ``agc_role``/``resource_type`` metadata wins.  Legacy AGC tables
    are already grouped before this function is called.  For generic model
    rows, an AC generator conservatively falls back to thermal support while a
    DC generator falls back to renewable support; deployments can remove that
    ambiguity by adding ``agc_role`` to the row.
    """

    token = _resource_text(device)
    if any(item in token for item in _RESOURCE_STORAGE_TOKENS):
        return "storage"
    if any(item in token for item in _RESOURCE_RENEWABLE_TOKENS):
        return "renewable"
    if any(item in token for item in _RESOURCE_THERMAL_TOKENS):
        return "diesel"
    return fallback


def _unique_devices(items: Iterable) -> List:
    output = []
    seen = set()
    for device in items:
        marker = id(device)
        if marker in seen:
            continue
        seen.add(marker)
        output.append(device)
    return output


def _device_side(device, default: str = "legacy") -> str:
    side = str(getattr(device, "agc_side", default) or default).strip().upper()
    return side if side in {"AC", "DC", "LEGACY"} else default.upper()


def _ensure_runtime_device(device, *, kind: str, side: str) -> None:
    """Add runtime fields to standard AC/DC rows while preserving model fields."""

    setattr(device, "agc_side", _device_side(device, side))
    if (side != "legacy") and (not hasattr(device, "p_cur") or getattr(device, "p_cur") is None):
        setattr(device, "p_cur", _power_value(device, "p", "p_set", default=0.0))
    if not hasattr(device, "p_ctrl") or getattr(device, "p_ctrl") is None:
        setattr(device, "p_ctrl", _as_float(getattr(device, "p_cur", 0.0)))
    if not hasattr(device, "p_min") or getattr(device, "p_min") is None:
        setattr(device, "p_min", 0.0)
    if not hasattr(device, "p_max") or getattr(device, "p_max") is None:
        current = _as_float(getattr(device, "p_cur", 0.0))
        setpoint = _as_float(getattr(device, "p_set", current))
        setattr(device, "p_max", max(0.0, current, setpoint))
    if kind == "storage":
        # Standard AC/DC generator rows do not define storage fields.  Keep a
        # neutral SOC until telemetry or an explicit row value is available;
        # the acceptance calculation still refuses a missing/invalid limit.
        if not hasattr(device, "soc_cur") or getattr(device, "soc_cur") is None:
            setattr(device, "soc_cur", 0.5)
        if not hasattr(device, "soc_min") or getattr(device, "soc_min") is None:
            setattr(device, "soc_min", 0.0)
        if not hasattr(device, "soc_max") or getattr(device, "soc_max") is None:
            setattr(device, "soc_max", 1.0)
        if not hasattr(device, "charge_p_max") or getattr(device, "charge_p_max") is None:
            setattr(device, "charge_p_max", max(0.0, _as_float(getattr(device, "p_max", 0.0))))
        if not hasattr(device, "dis_charge_p_max") or getattr(device, "dis_charge_p_max") is None:
            setattr(device, "dis_charge_p_max", max(0.0, _as_float(getattr(device, "p_max", 0.0))))


def _ensure_control_limits(model, device, kind: str) -> None:
    """Expose normalized step/deadband fields on every AGC-controlled row."""

    if getattr(device, "agc_control_step", None) is None:
        setattr(device, "agc_control_step", _control_step(model, device, kind))
    if getattr(device, "agc_control_deadband", None) is None:
        setattr(device, "agc_control_deadband", _control_deadband(model, device, kind))


def _agc_resource_groups(model) -> Dict[str, List]:
    """Return de-duplicated renewable/storage/diesel resources across AC and DC.

    The legacy four lists remain valid.  Standard model objects are added by
    role metadata, so AC and DC devices share exactly the same dispatch path.
    """

    cached = getattr(model, "_agc_resource_groups", None)
    if isinstance(cached, dict):
        return cached

    renewable = []
    storage = []
    diesel = []
    for device in getattr(model, "wind_generator", []):
        _ensure_runtime_device(device, kind="renewable", side="legacy")
        renewable.append(device)
    for device in getattr(model, "pv_generator", []):
        _ensure_runtime_device(device, kind="renewable", side="legacy")
        renewable.append(device)
    for device in getattr(model, "estorage", []):
        _ensure_runtime_device(device, kind="storage", side="legacy")
        storage.append(device)
    for device in getattr(model, "diesel_generator", []):
        _ensure_runtime_device(device, kind="diesel", side="legacy")
        diesel.append(device)

    for table_name, side in (("ACGenerator", "AC"), ("ACUnit", "AC"), ("DCGenerator", "DC"), ("DCUnit", "DC")):
        for device in getattr(model, table_name, []) or []:
            fallback = "diesel" if side == "AC" else "renewable"
            kind = _classify_resource(device, side=side, fallback=fallback)
            if kind not in {"renewable", "storage", "diesel"}:
                continue
            _ensure_runtime_device(device, kind=kind, side=side)
            if kind == "renewable":
                renewable.append(device)
            elif kind == "storage":
                storage.append(device)
            else:
                diesel.append(device)

    groups = {
        "renewable": _unique_devices(renewable),
        "storage": _unique_devices(storage),
        "diesel": _unique_devices(diesel),
    }
    for kind, devices in groups.items():
        for device in devices:
            _ensure_control_limits(model, device, kind)
    model._agc_resource_groups = groups
    model.renew_generator = list(groups["renewable"])
    model.agc_renewable_generator = list(groups["renewable"])
    model.agc_storage = list(groups["storage"])
    model.agc_diesel_generator = list(groups["diesel"])
    return groups


def _prepare_agc_resources(model, logger: logging.Logger, yc_by_id: Dict[str, object]) -> None:
    groups = _agc_resource_groups(model)
    for kind, devices in groups.items():
        for device in devices:
            p_yc_id = getattr(device, "p_yc_id", None)
            if p_yc_id not in (None, "") and getattr(device, "p_yc", None) is None:
                device.p_yc = _find_by_id(yc_by_id, p_yc_id, logger, f"{kind} 遥测")
            soc_yc_id = getattr(device, "soc_yc_id", None)
            if kind == "storage" and soc_yc_id not in (None, "") and getattr(device, "soc_yc", None) is None:
                device.soc_yc = _find_by_id(yc_by_id, soc_yc_id, logger, "储能 SOC 遥测")
    for yt in getattr(model, "yt", []) or []:
        role = str(getattr(yt, "control_role", "")).strip().upper()
        if role == YT_ROLE_FUEL_CELL_SUPPORT:
            _ensure_control_limits(model, yt, "fuel_cell")
        elif role == YT_ROLE_HYDROGEN_ABSORB:
            _ensure_control_limits(model, yt, "hydrogen")
    logger.info(
        "AGC 资源识别: AC/DC 新能源=%s, 储能=%s, 柴发/常规机组=%s",
        len(groups["renewable"]), len(groups["storage"]), len(groups["diesel"]),
    )


def _telemetry_value(yobj, default: float = 0.0) -> float:
    if yobj is None:
        return default
    value = getattr(yobj, "value", default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _power_value(device, *attrs: str, default: float = 0.0) -> float:
    """Read the first usable power field, skipping unset ``None`` values."""

    for attr_name in attrs:
        value = getattr(device, attr_name, None)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return float(default)


def _is_running(device) -> bool:
    return int(getattr(device, "run_stat", 1) or 0) == 1


def _clamp(value: float, lower: float, upper: float) -> float:
    if upper < lower:
        lower, upper = upper, lower
    return max(lower, min(upper, value))


def _current_control_value(device) -> float:
    return _power_value(device, "p_ctrl", "p_cur", "p", "p_set", default=0.0)


def _is_remote_controlled(device) -> bool:
    """Whether automatic AGC is allowed to change this device."""
    state = str(getattr(device, "agc_control_state", "remote") or "remote").strip().lower()
    return state not in {"local", "本地", "l"}


def _device_float(device, attr_names: Tuple[str, ...], default=None):
    for attr_name in attr_names:
        value = _as_float(getattr(device, attr_name, None), None)
        if value is not None:
            return value
    return default


def _control_step(model, device, kind: str) -> float:
    device_step = _device_float(device, ("agc_control_step", "control_step", "p_step"), None)
    if device_step is not None and device_step >= 0.0:
        return device_step
    for parameter_name in (
        f"{kind.upper()}_CONTROL_STEP",
        f"{kind.upper()}_STEP",
        "CONTROL_STEP",
    ):
        specific = get_para_float(model, parameter_name, -1.0)
        if specific >= 0.0:
            return specific
    return get_para_float(model, "CONTROL_STEP", DEFAULT_CONTROL_STEP)


def _control_deadband(model, device, kind: str) -> float:
    """Return the per-device active-power deadband for AGC commands."""

    device_deadband = _device_float(
        device,
        ("agc_control_deadband", "control_deadband", "p_dead", "deadband"),
        None,
    )
    if device_deadband is not None and device_deadband >= 0.0:
        return device_deadband
    for parameter_name in (
        f"{kind.upper()}_CONTROL_DEADBAND",
        f"{kind.upper()}_DEADBAND",
        "CONTROL_DEADBAND",
    ):
        configured = get_para_float(model, parameter_name, -1.0)
        if configured >= 0.0:
            return configured
    return DEFAULT_CONTROL_DEADBAND


def _limit_control_step(model, device, target: float, kind: str) -> float:
    before = _current_control_value(device)
    step = _control_step(model, device, kind)
    if step < 0.0:
        return target
    return _clamp(target, before - step, before + step)


def _set_p_ctrl(
    model,
    device,
    target: float,
    kind: str,
    lower: float,
    upper: float,
) -> Tuple[float, float]:
    before = _current_control_value(device)
    if not _is_remote_controlled(device):
        # Local devices retain the setpoint supplied by their preset curve.
        # Keep them in resource groups so balance checks still include output.
        return before, before
    bounded = _clamp(target, lower, upper)
    deadband = _control_deadband(model, device, kind)
    if _is_running(device) and abs(bounded - before) <= deadband:
        after = before
    else:
        after = _clamp(_limit_control_step(model, device, bounded, kind), lower, upper)
    device.p_ctrl = after
    _apply_model_power_setpoint(device, after)
    return before, after


def _apply_model_power_setpoint(device, value: float) -> None:
    """Mirror a control command to standard AC/DC model setpoint fields."""

    if hasattr(device, "p_set"):
        device.p_set = float(value)
    command_attr = getattr(device, "agc_power_setpoint_attr", None)
    if command_attr:
        setattr(device, command_attr, float(value))


def _probe_step(model, device, kind: str) -> float:
    configured = _device_float(device, ("agc_probe_step",), None)
    if configured is None:
        configured = get_para_float(model, "AGC_PROBE_STEP", DEFAULT_AGC_PROBE_STEP)
    return min(max(0.0, configured), max(0.0, _control_step(model, device, kind)))


def _probe_set_p_ctrl(
    model,
    device,
    target: float,
    kind: str,
    lower: float,
    upper: float,
) -> Tuple[float, float]:
    """Move one device by one small trial step, never by a full mismatch."""

    before = _current_control_value(device)
    step = _probe_step(model, device, kind)
    if step <= 0.0:
        return before, before
    bounded = _clamp(target, lower, upper)
    delta = _clamp(bounded - before, -step, step)
    return _set_p_ctrl(model, device, before + delta, kind, lower, upper)


def _sum_attr(devices: Iterable, attr_name: str) -> float:
    return sum(_as_float(getattr(device, attr_name, 0.0)) for device in devices if _is_running(device))


def _running_diesel_output(model) -> float:
    return sum(
        _as_float(getattr(gen, "p_ctrl", getattr(gen, "p_cur", 0.0)))
        for gen in _agc_resource_groups(model)["diesel"]
        if _is_running(gen)
    )


def _diesel_min_deadband(model, gen) -> float:
    device_deadband = _device_float(gen, ("p_dead",), None)
    if device_deadband is not None:
        return max(0.0, device_deadband)
    return max(0.0, get_para_float(model, "DIESEL_MIN_DEADBAND", DEFAULT_DIESEL_MIN_DEADBAND))


def _running_diesel_min_bounds(model) -> Tuple[float, float, float]:
    diesel_min = 0.0
    lower = 0.0
    upper = 0.0
    for gen in _agc_resource_groups(model)["diesel"]:
        if not _is_running(gen):
            continue
        p_min = _as_float(getattr(gen, "p_min", 0.0))
        deadband = _diesel_min_deadband(model, gen)
        diesel_min += p_min
        lower += p_min - deadband
        upper += p_min + deadband
    return diesel_min, lower, upper


def _renewable_output(model) -> float:
    return _sum_attr(_agc_resource_groups(model)["renewable"], "p_ctrl")


def _measured_output(devices: Iterable) -> float:
    """Return measured active power; this intentionally never reads p_fur."""

    return sum(
        _power_value(device, "p_cur", "p", "p_ctrl", "p_set", default=0.0)
        for device in devices
        if _is_running(device)
    )


def _all_load_devices(model) -> List:
    loads = list(getattr(model, "energyconsumer", []) or [])
    for table_name in ("ACLoad", "DCLoad"):
        loads.extend(getattr(model, table_name, []) or [])
    return _unique_devices(loads)


def _load_target(model) -> float:
    # Forecast fields are deliberately excluded from AGC decisions.  For the
    # legacy energyconsumer table, p_cur is refreshed by data_gather.  Standard
    # AC/DC loads use p or pbase as their local measured/setpoint fallback.
    load = 0.0
    for device in _all_load_devices(model):
        if not _is_running(device):
            continue
        value = getattr(device, "p_cur", None)
        if value is None:
            value = _power_value(device, "p", "pbase", "p_set", default=0.0)
        load += _as_float(value)
    return max(0.0, load)


def _controlled_power_mismatch(model) -> float:
    load = _load_target(model)
    generation = (
        _renewable_output(model)
        + _running_diesel_output(model)
        + _sum_attr(_agc_resource_groups(model)["storage"], "p_ctrl")
    )
    return load - generation


def _format_float(value) -> str:
    return f"{_as_float(value):.6f}"


def _format_ctrl_value(value) -> str:
    if isinstance(value, str):
        return value
    return _format_float(value)


def _dev_ctrl_rows(model) -> Iterable[Tuple[str, object]]:
    groups = _agc_resource_groups(model)
    legacy_labels = (
        ("wind_generator", getattr(model, "wind_generator", [])),
        ("pv_generator", getattr(model, "pv_generator", [])),
        ("estorage", getattr(model, "estorage", [])),
        ("diesel_generator", getattr(model, "diesel_generator", [])),
    )
    emitted = set()
    for dev_type, devices in legacy_labels:
        for device in devices:
            emitted.add(id(device))
            yield dev_type, device
    for dev_type, devices in (
        ("ac_dc_renewable", groups["renewable"]),
        ("ac_dc_storage", groups["storage"]),
        ("ac_dc_diesel", groups["diesel"]),
    ):
        for device in devices:
            if id(device) not in emitted:
                emitted.add(id(device))
                yield dev_type, device


def _format_dev_ctrl_row(prefix: str, dev_type, dev_id, name, p_ctrl, p_real) -> str:
    return f"{prefix} {str(dev_type):<16} {str(dev_id):<6} {str(name):<10} {_format_ctrl_value(p_ctrl):<10} {_format_ctrl_value(p_real)}"


def _real_power(device) -> float:
    return _as_float(getattr(device, "p_cur", getattr(device, "p_ctrl", 0.0)))


def _format_yt_ctrl_row(prefix: str, yt_id, name, rtu, pnt, value) -> str:
    return f"{prefix} {str(yt_id):<6} {str(name):<10} {str(rtu):<6} {str(pnt):<6} {_format_ctrl_value(value)}"


def _format_result_row(prefix: str, name, value) -> str:
    return f"{prefix} {str(name):<20} {_format_ctrl_value(value)}"


def export_control_results(model, output_file: Path) -> None:
    output_file = Path(output_file)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "<dev_ctrl>",
        _format_dev_ctrl_row("@", "dev_type", "id", "name", "p_ctrl", "p_real"),
    ]
    for dev_type, device in _dev_ctrl_rows(model):
        lines.append(
            _format_dev_ctrl_row(
                "#",
                dev_type,
                getattr(device, "id", ""),
                getattr(device, "name", ""),
                getattr(device, "p_ctrl", getattr(device, "p_cur", 0.0)),
                _real_power(device),
            )
        )
    lines.extend(
        [
            "</dev_ctrl>",
            "",
            "<yt_ctrl>",
            _format_yt_ctrl_row("@", "id", "name", "rtu", "pnt", "value"),
        ]
    )
    for yt in getattr(model, "yt", []):
        ctrl_value = getattr(yt, "ctrl_value", None)
        if ctrl_value is None:
            continue
        lines.append(
            _format_yt_ctrl_row(
                "#",
                getattr(yt, "id", ""),
                getattr(yt, "name", ""),
                getattr(yt, "rtu", ""),
                getattr(yt, "pnt", ""),
                ctrl_value,
            )
        )
    lines.extend(
        [
            "</yt_ctrl>",
            "",
            "<agc_result>",
            _format_result_row("@", "name", "value"),
            _format_result_row("#", "agc_balance_mismatch", getattr(model, "agc_balance_mismatch", 0.0)),
            "</agc_result>",
            "",
        ]
    )
    output_file.write_text("\n".join(lines), encoding="utf-8")


def _storage_soc_deadband(model, estore) -> float:
    device_deadband = _device_float(estore, ("soc_dead",), None)
    if device_deadband is not None:
        return max(0.0, device_deadband)
    return max(0.0, get_para_float(model, "STORAGE_SOC_DEADBAND", DEFAULT_STORAGE_SOC_DEADBAND))


def _can_charge_storage(model, estore) -> bool:
    return _storage_soc_fraction(
        estore,
        charging=True,
        deadband=_storage_soc_deadband(model, estore),
    ) > 0.0


def _can_discharge_storage(model, estore) -> bool:
    return _storage_soc_fraction(
        estore,
        charging=False,
        deadband=_storage_soc_deadband(model, estore),
    ) > 0.0


def _storage_energy_capacity(estore) -> Optional[float]:
    for attr_name in ("energy_capacity", "capacity_mwh", "e_max", "soc_capacity"):
        value = _device_float(estore, (attr_name,), None)
        if value is not None and value > 0.0:
            return value
    return None


def _storage_soc_fraction(estore, *, charging: bool, deadband: float) -> float:
    """Return remaining SOC room as a normalized fraction of usable range.

    Legacy E-files commonly store SOC as 0..100 while newer model rows may
    use 0..1.  Normalizing by ``soc_max - soc_min`` makes both conventions
    equivalent and prevents a percentage-point value from being treated as a
    unit fraction.
    """

    soc = _as_float(getattr(estore, "soc_cur", 0.0), 0.0)
    soc_min = _as_float(getattr(estore, "soc_min", 0.0), 0.0)
    soc_max = _as_float(getattr(estore, "soc_max", 1.0), 1.0)
    # Standard AC/DC rows do not carry SOC bounds.  If their telemetry is a
    # percentage while the default bound is 1.0, infer the conventional
    # 0..100 range without changing an explicitly supplied 0..1 model.
    if soc_max <= 1.0 and soc > 1.0:
        soc_min, soc_max = soc_min * 100.0, soc_max * 100.0
    usable = soc_max - soc_min
    if usable <= 0.0:
        return 0.0
    if charging:
        room = max(0.0, soc_max - deadband - soc)
    else:
        room = max(0.0, soc - soc_min - deadband)
    return _clamp(room / usable, 0.0, 1.0)


def _agc_acceptance_horizon_h(model) -> float:
    configured = get_para_float(model, "AGC_ACCEPTANCE_HORIZON_H", -1.0)
    if configured > 0.0:
        return configured
    trigger = _as_float(getattr(model, "ctrl_trigger", DEFAULT_CTRL_TRIGGER), DEFAULT_CTRL_TRIGGER)
    return max(1e-6, trigger / 3600.0, DEFAULT_ACCEPTANCE_HORIZON_H)


def _storage_soc_power_limit(model, estore, *, charging: bool) -> float:
    """Return power headroom allowed by SOC and the next AGC control horizon."""

    if not _is_running(estore):
        return 0.0
    dead = _storage_soc_deadband(model, estore)
    fraction = _storage_soc_fraction(estore, charging=charging, deadband=dead)
    if fraction <= 0.0:
        return 0.0
    if charging:
        power_limit = max(0.0, _as_float(getattr(estore, "charge_p_max", 0.0)))
    else:
        power_limit = max(0.0, _as_float(getattr(estore, "dis_charge_p_max", 0.0)))
    capacity = _storage_energy_capacity(estore)
    if capacity is not None:
        horizon_h = _agc_acceptance_horizon_h(model)
        power_limit = min(power_limit, capacity * fraction / horizon_h)
    return max(0.0, power_limit)


def _storage_charge_acceptance_space(model) -> float:
    return sum(
        _storage_soc_power_limit(model, estore, charging=True)
        for estore in _agc_resource_groups(model)["storage"]
    )


def _storage_incremental_acceptance_space(model) -> float:
    """Return storage absorption headroom measured from each current command."""

    total = 0.0
    for estore in _agc_resource_groups(model)["storage"]:
        charge_limit = _storage_soc_power_limit(model, estore, charging=True)
        if charge_limit <= 0.0:
            continue
        current = _current_control_value(estore)
        lower = -charge_limit
        total += max(0.0, current - lower)
    return total


def _storage_discharge_space(model) -> float:
    return sum(
        _storage_soc_power_limit(model, estore, charging=False)
        for estore in _agc_resource_groups(model)["storage"]
    )


def calculate_renewable_acceptance_space(model) -> Dict[str, float]:
    """Calculate real-time renewable acceptance without using ``p_fur``.

    The space includes measured load headroom, diesel output reducible above
    its minimum, and storage charging headroom permitted by SOC.  It is a
    power-space estimate for the next short AGC trial, not a forecast.
    """

    groups = _agc_resource_groups(model)
    load = _load_target(model)
    renewable_actual = _measured_output(groups["renewable"])
    diesel_actual = _measured_output(groups["diesel"])
    diesel_min = sum(
        max(0.0, _as_float(getattr(gen, "p_min", 0.0)))
        for gen in groups["diesel"]
        if _is_running(gen)
    )
    diesel_reduction = max(0.0, diesel_actual - diesel_min)
    storage_charge = _storage_incremental_acceptance_space(model)
    # Renewable power can replace load, reduce thermal output to its minimum,
    # or be absorbed by storage.  Existing renewable output is subtracted.
    acceptance = max(0.0, load - diesel_min + storage_charge - renewable_actual)
    details = {
        "load": load,
        "renewable_actual": renewable_actual,
        "diesel_actual": diesel_actual,
        "diesel_min": diesel_min,
        "diesel_reduction": diesel_reduction,
        "storage_charge_space": storage_charge,
        "storage_incremental_acceptance_space": storage_charge,
        "acceptance_space": acceptance,
    }
    model.agc_renewable_acceptance = details
    model.agc_renewable_acceptance_space = acceptance
    return details


def _storage_by_soc(model, reverse: bool) -> list:
    return sorted(
        _agc_resource_groups(model)["storage"],
        key=lambda estore: _as_float(getattr(estore, "soc_cur", 0.0)),
        reverse=reverse,
    )


def _find_first_by_control_role(items: Iterable, control_role: str):
    expected_role = str(control_role).strip().upper()
    for item in items:
        item_role = str(getattr(item, "control_role", "")).strip().upper()
        if item_role == expected_role:
            return item
    return None


def do_renew_gen_fur_calc_default(logger, model, wind_speed: float, solar_irrad: float, env_temp: float) -> None:
    """Forecast renewable active power for the next control period.

    ``p_fur`` remains available for reporting/forecast consumers.  The default
    AGC decision path deliberately does not use it; it uses measured output and
    the acceptance-space probe in :func:`do_wind_solar_priority_ctrl`.
    """
    solar_irrad = max(0.0, _as_float(solar_irrad))
    wind_speed = max(0.0, _as_float(wind_speed))
    env_temp = _as_float(env_temp, 25.0)

    for pv in getattr(model, "pv_generator", []):
        p_max = _as_float(getattr(pv, "p_max", 0.0))
        p_min = _as_float(getattr(pv, "p_min", 0.0))
        rated = _as_float(getattr(pv, "rated_power", p_max), p_max)
        ref_irrad = max(_as_float(getattr(pv, "reference_irradiance", 1000.0), 1000.0), 1e-6)
        ref_temp = _as_float(getattr(pv, "reference_temperature", 25.0), 25.0)
        temp_coef = _as_float(getattr(pv, "temp_coefficient", 0.0), 0.0)
        raw = rated * solar_irrad / ref_irrad * (1.0 + temp_coef * (env_temp - ref_temp))
        pv.p_fur = _clamp(raw if _is_running(pv) else 0.0, p_min, p_max)
        logger.info("光伏预测 %s p_fur=%.3f", pv.name, pv.p_fur)

    for wind in getattr(model, "wind_generator", []):
        p_max = _as_float(getattr(wind, "p_max", 0.0))
        p_min = _as_float(getattr(wind, "p_min", 0.0))
        rated = _as_float(getattr(wind, "rated_power", p_max), p_max)
        cut_in = _as_float(getattr(wind, "cut_in_speed", 0.0), 0.0)
        cut_out = _as_float(getattr(wind, "cut_out_speed", 1e9), 1e9)
        rated_speed = max(_as_float(getattr(wind, "rated_wind_speed", cut_out), cut_out), cut_in + 1e-6)
        if not _is_running(wind) or wind_speed < cut_in or wind_speed >= cut_out:
            raw = 0.0
        elif wind_speed >= rated_speed:
            raw = rated
        else:
            ratio = (wind_speed - cut_in) / (rated_speed - cut_in)
            raw = rated * ratio ** 3
        wind.p_fur = _clamp(raw, p_min, p_max)
        logger.info("风机预测 %s p_fur=%.3f", wind.name, wind.p_fur)


def _dispatch_renewable_priority(logger, model, load_target: float) -> float:
    """Dispatch wind and PV first and return remaining load deficit."""
    demand = max(0.0, load_target)
    renewable_units = []
    for group_name, devices in (("风机", getattr(model, "wind_generator", [])), ("光伏", getattr(model, "pv_generator", []))):
        for gen in devices:
            if not _is_running(gen):
                gen.p_ctrl = 0.0
                continue
            available = max(0.0, _as_float(getattr(gen, "p_fur", getattr(gen, "p_cur", 0.0))))
            p_max = _as_float(getattr(gen, "p_max", available), available)
            renewable_units.append((group_name, gen, min(available, p_max), p_max))

    total_available = sum(available for _, _, available, _ in renewable_units)
    if total_available <= 0.0:
        return demand

    scale = min(1.0, demand / total_available)
    supplied = 0.0
    for group_name, gen, available, p_max in renewable_units:
        target = available * scale
        before, after = _set_p_ctrl(model, gen, target, "renew", 0.0, p_max)
        supplied += after
        logger.info(
            "%s协同出力 %s p_ctrl %.3f -> %.3f p_fur=%.3f",
            group_name,
            gen.name,
            before,
            after,
            available,
        )
    remaining = max(0.0, demand - supplied)
    return remaining


def _charge_storage(logger, model, surplus: float) -> float:
    remaining = max(0.0, surplus)
    for estore in _storage_by_soc(model, reverse=False):
        if not _is_running(estore):
            estore.p_ctrl = 0.0
            continue
        soc = _as_float(getattr(estore, "soc_cur", 0.0))
        if not _can_charge_storage(model, estore):
            estore.p_ctrl = 0.0
            continue
        limit = _as_float(getattr(estore, "charge_p_max", 0.0))
        target = -min(remaining, limit)
        before, after = _set_p_ctrl(model, estore, target, "storage", -limit, limit)
        absorbed = max(0.0, -after)
        remaining -= absorbed
        logger.info("储能优先充电 %s p_ctrl %.3f -> %.3f soc=%.3f", estore.name, before, after, soc)
        if remaining <= 0.0:
            break
    return remaining


def _discharge_storage(logger, model, deficit: float) -> float:
    remaining = max(0.0, deficit)
    for estore in _storage_by_soc(model, reverse=True):
        if not _is_running(estore):
            estore.p_ctrl = 0.0
            continue
        soc = _as_float(getattr(estore, "soc_cur", 0.0))
        if not _can_discharge_storage(model, estore):
            estore.p_ctrl = 0.0
            continue
        limit = _as_float(getattr(estore, "dis_charge_p_max", 0.0))
        target = min(remaining, limit)
        before, after = _set_p_ctrl(model, estore, target, "storage", -limit, limit)
        supplied = max(0.0, after)
        remaining -= supplied
        logger.info("储能补缺放电 %s p_ctrl %.3f -> %.3f soc=%.3f", estore.name, before, after, soc)
        if remaining <= 0.0:
            break
    return remaining


def _dispatch_diesel(logger, model, deficit: float) -> float:
    remaining = max(0.0, deficit)
    for gen in getattr(model, "diesel_generator", []):
        if not _is_running(gen):
            lower = _as_float(getattr(gen, "p_min", 0.0))
            _set_p_ctrl(model, gen, lower, "renew", lower, max(lower, _as_float(getattr(gen, "p_max", lower))))
            continue
        p_min = _as_float(getattr(gen, "p_min", 0.0))
        p_max = _as_float(getattr(gen, "p_max", 0.0))
        target = _clamp(remaining, p_min if remaining >= p_min else 0.0, p_max)
        before, after = _set_p_ctrl(model, gen, target, "diesel", 0.0, p_max)
        remaining -= after
        logger.info("柴发补缺 %s p_ctrl %.3f -> %.3f", gen.name, before, after)
        if remaining <= 0.0:
            break
    return remaining


def _curtail_renewable(logger, model, surplus: float) -> float:
    remaining = max(0.0, surplus)
    for devices in (getattr(model, "pv_generator", []), getattr(model, "wind_generator", [])):
        for gen in devices:
            if remaining <= 0.0:
                return 0.0
            before = _as_float(getattr(gen, "p_ctrl", 0.0))
            lower = _as_float(getattr(gen, "p_min", 0.0))
            reducible = max(0.0, before - lower)
            if reducible <= 0.0:
                continue
            target = before - min(reducible, remaining)
            _, after = _set_p_ctrl(model, gen, target, "renew", lower, before)
            reduced = before - after
            remaining -= reduced
            logger.info("富余无法消纳，弃风弃光 %s p_ctrl %.3f -> %.3f", gen.name, before, after)
    return remaining


def _restore_renewable(logger, model, shortage: float) -> float:
    remaining = max(0.0, shortage)
    for devices in (getattr(model, "wind_generator", []), getattr(model, "pv_generator", [])):
        for gen in devices:
            if remaining <= 0.0:
                return 0.0
            if not _is_running(gen):
                continue
            before = _as_float(getattr(gen, "p_ctrl", 0.0))
            available = _as_float(getattr(gen, "p_fur", before), before)
            headroom = max(0.0, available - before)
            if headroom <= 0.0:
                continue
            target = before + min(headroom, remaining)
            _, after = _set_p_ctrl(model, gen, target, "renew", before, available)
            increased = after - before
            remaining -= increased
            logger.info("减少新能源弃电 %s p_ctrl %.3f -> %.3f", gen.name, before, after)
    return remaining


def _reduce_storage_discharge(logger, model, surplus: float) -> float:
    remaining = max(0.0, surplus)
    for estore in _storage_by_soc(model, reverse=False):
        if remaining <= 0.0:
            return 0.0
        before = _as_float(getattr(estore, "p_ctrl", 0.0))
        if not _is_running(estore) or before <= 0.0:
            continue
        target = before - min(before, remaining)
        _, after = _set_p_ctrl(model, estore, target, "storage", 0.0, before)
        reduced = before - after
        remaining -= reduced
        logger.info("柴发低于下限，减少储能放电 %s p_ctrl %.3f -> %.3f", estore.name, before, after)
    return remaining


def _reduce_storage_charge(logger, model, shortage: float) -> float:
    remaining = max(0.0, shortage)
    for estore in _storage_by_soc(model, reverse=True):
        if remaining <= 0.0:
            return 0.0
        before = _as_float(getattr(estore, "p_ctrl", 0.0))
        if not _is_running(estore) or before >= 0.0:
            continue
        target = before + min(-before, remaining)
        _, after = _set_p_ctrl(model, estore, target, "storage", before, 0.0)
        increased = after - before
        remaining -= increased
        logger.info("柴发高于下限，减少储能充电 %s p_ctrl %.3f -> %.3f", estore.name, before, after)
    return remaining


def _force_storage_charge(logger, model, surplus: float) -> float:
    remaining = max(0.0, surplus)
    for estore in _storage_by_soc(model, reverse=False):
        if remaining <= 0.0:
            return 0.0
        if not _is_running(estore):
            continue
        if not _can_charge_storage(model, estore):
            continue
        before = _as_float(getattr(estore, "p_ctrl", 0.0))
        limit = _as_float(getattr(estore, "charge_p_max", 0.0))
        headroom = max(0.0, limit + min(0.0, before))
        charge = min(headroom, remaining)
        if charge <= 0.0:
            continue
        target = before - charge
        _, after = _set_p_ctrl(model, estore, target, "storage", -limit, limit)
        actual_charge = before - after
        remaining -= actual_charge
        logger.info("新能源已降至下限，储能转充电 %s p_ctrl %.3f -> %.3f", estore.name, before, after)
    return remaining


def _force_storage_discharge(logger, model, shortage: float) -> float:
    remaining = max(0.0, shortage)
    for estore in _storage_by_soc(model, reverse=True):
        if remaining <= 0.0:
            return 0.0
        if not _is_running(estore):
            continue
        if not _can_discharge_storage(model, estore):
            continue
        before = _as_float(getattr(estore, "p_ctrl", 0.0))
        limit = _as_float(getattr(estore, "dis_charge_p_max", 0.0))
        headroom = max(0.0, limit - max(0.0, before))
        discharge = min(headroom, remaining)
        if discharge <= 0.0:
            continue
        target = before + discharge
        _, after = _set_p_ctrl(model, estore, target, "storage", -limit, limit)
        actual_discharge = after - before
        remaining -= actual_discharge
        logger.info("新能源已恢复至最大可发，储能转放电 %s p_ctrl %.3f -> %.3f", estore.name, before, after)
    return remaining


def _apply_diesel_min_output_coordination(logger, model) -> None:
    diesel_min, diesel_lower, diesel_upper = _running_diesel_min_bounds(model)
    if diesel_min <= 0.0:
        return
    diesel_output = _running_diesel_output(model)

    if diesel_output < diesel_lower:
        excess = diesel_lower - diesel_output
        logger.info(
            "柴发出力 %.3f 低于下限死区边界 %.3f，按储能放电->新能源弃电->储能充电修正",
            diesel_output,
            diesel_lower,
        )
        excess = _reduce_storage_discharge(logger, model, excess)
        excess = _curtail_renewable(logger, model, excess)
        excess = _force_storage_charge(logger, model, excess)
    elif diesel_output > diesel_upper:
        shortage = diesel_output - diesel_upper
        logger.info(
            "柴发出力 %.3f 高于下限死区边界 %.3f，按储能充电->减少弃电->储能放电修正",
            diesel_output,
            diesel_upper,
        )
        shortage = _reduce_storage_charge(logger, model, shortage)
        shortage = _restore_renewable(logger, model, shortage)
        shortage = _force_storage_discharge(logger, model, shortage)
    else:
        mismatch = _controlled_power_mismatch(model)
        if mismatch > DEFAULT_BALANCE_DEADBAND:
            logger.info(
                "柴发出力 %.3f 位于下限死区内，功率缺额 %.3f 由新能源+储能联合补足",
                diesel_output,
                mismatch,
            )
            mismatch = _restore_renewable(logger, model, mismatch)
            mismatch = _force_storage_discharge(logger, model, mismatch)
        elif mismatch < -DEFAULT_BALANCE_DEADBAND:
            surplus = -mismatch
            logger.info(
                "柴发出力 %.3f 位于下限死区内，功率富余 %.3f 由储能+新能源联合消纳",
                diesel_output,
                surplus,
            )
            surplus = _reduce_storage_discharge(logger, model, surplus)
            surplus = _curtail_renewable(logger, model, surplus)
            surplus = _force_storage_charge(logger, model, surplus)


def _send_hydrogen_absorb(logger, model, surplus: float) -> float:
    if surplus <= 0.0:
        return 0.0
    yt = _find_first_by_control_role(
        getattr(model, "yt", []),
        YT_ROLE_HYDROGEN_ABSORB,
    )
    if yt is None:
        return surplus
    model.config.send_yt_cmd(yt, surplus)
    logger.info("风光富余转制氢 %s value=%.3f", yt.name, surplus)
    return 0.0


def _probe_hydrogen_absorb(logger, model, surplus: float) -> float:
    """Increase hydrogen absorption by one bounded trial step."""

    if surplus <= 0.0:
        return 0.0
    yt = _find_first_by_control_role(getattr(model, "yt", []), YT_ROLE_HYDROGEN_ABSORB)
    if yt is None:
        return surplus
    step = _device_float(yt, ("agc_control_step", "control_step", "p_step"), None)
    if step is None:
        step = get_para_float(
            model,
            "HYDROGEN_CONTROL_STEP",
            get_para_float(model, "HYDROGEN_PROBE_STEP", get_para_float(model, "AGC_PROBE_STEP", DEFAULT_AGC_PROBE_STEP)),
        )
    deadband = _control_deadband(model, yt, "hydrogen")
    if surplus <= deadband:
        return surplus
    current = _device_float(yt, ("ctrl_value", "p_ctrl"), 0.0)
    lower = _device_float(yt, ("p_min",), 0.0)
    upper = _device_float(yt, ("p_max",), None)
    target = max(lower, current + min(surplus, max(0.0, step)))
    if upper is not None:
        target = min(target, upper)
    absorbed = max(0.0, target - current)
    if absorbed <= 0.0:
        return surplus
    model.config.send_yt_cmd(yt, target)
    logger.info("风光富余转制氢小步吸收 %s ctrl=%.3f -> %.3f residual=%.3f", yt.name, current, target, surplus - absorbed)
    return max(0.0, surplus - absorbed)


def _send_fuel_cell_support(logger, model, deficit: float) -> float:
    if deficit <= 0.0:
        return 0.0
    yt = _find_first_by_control_role(
        getattr(model, "yt", []),
        YT_ROLE_FUEL_CELL_SUPPORT,
    )
    if yt is None:
        return deficit
    model.config.send_yt_cmd(yt, deficit)
    logger.info("缺额转燃料电池支撑 %s value=%.3f", yt.name, deficit)
    return 0.0


def _probe_fuel_cell_support(logger, model, deficit: float) -> float:
    """Increase fuel-cell support by one trial step and return the residual."""

    if deficit <= 0.0:
        return 0.0
    yt = _find_first_by_control_role(
        getattr(model, "yt", []),
        YT_ROLE_FUEL_CELL_SUPPORT,
    )
    if yt is None:
        return deficit
    step = _device_float(yt, ("agc_probe_step", "agc_control_step", "control_step", "p_step"), None)
    if step is None:
        step = get_para_float(
            model,
            "FUEL_CELL_CONTROL_STEP",
            get_para_float(model, "FUEL_CELL_PROBE_STEP", get_para_float(model, "AGC_PROBE_STEP", DEFAULT_AGC_PROBE_STEP)),
        )
    step = max(0.0, step)
    if step <= 0.0 or deficit <= _control_deadband(model, yt, "fuel_cell"):
        return deficit
    current = _device_float(yt, ("ctrl_value", "p_ctrl"), 0.0)
    lower = _device_float(yt, ("p_min",), 0.0)
    upper = _device_float(yt, ("p_max",), None)
    target = max(lower, current + min(deficit, step))
    if upper is not None:
        target = min(target, upper)
    delivered = max(0.0, target - current)
    if delivered <= 0.0:
        return deficit
    model.config.send_yt_cmd(yt, target)
    logger.info("缺额转燃料电池小步支撑 %s ctrl=%.3f -> %.3f residual=%.3f", yt.name, current, target, deficit - delivered)
    return max(0.0, deficit - delivered)


def _probe_dispatch_renewables(logger, model, acceptance_space: float) -> float:
    """Increase renewable commands by one measured small-step trial."""

    remaining = max(0.0, acceptance_space)
    for gen in _agc_resource_groups(model)["renewable"]:
        if remaining <= DEFAULT_BALANCE_DEADBAND:
            break
        if not _is_running(gen):
            upper = max(0.0, _as_float(getattr(gen, "p_max", 0.0)))
            _set_p_ctrl(model, gen, 0.0, "renew", 0.0, upper)
            continue
        lower = _as_float(getattr(gen, "p_min", 0.0))
        upper = max(lower, _as_float(getattr(gen, "p_max", 0.0)))
        before = _current_control_value(gen)
        measured = _as_float(getattr(gen, "p_cur", before), before)
        baseline = max(before, measured, lower)
        headroom = max(0.0, upper - baseline)
        if headroom <= 0.0:
            continue
        target = baseline + min(headroom, remaining)
        before, after = _probe_set_p_ctrl(model, gen, target, "renew", lower, upper)
        increase = max(0.0, after - before)
        remaining -= increase
        logger.info(
            "新能源小步试探 %s[%s] p_ctrl %.3f -> %.3f measured=%.3f acceptance_left=%.3f",
            getattr(gen, "name", getattr(gen, "idx", "")),
            _device_side(gen),
            before,
            after,
            measured,
            remaining,
        )
    return remaining


def _probe_storage_charge(logger, model, surplus: float) -> float:
    remaining = max(0.0, surplus)
    for estore in _storage_by_soc(model, reverse=False):
        if remaining <= DEFAULT_BALANCE_DEADBAND:
            break
        headroom = _storage_soc_power_limit(model, estore, charging=True)
        if headroom <= 0.0:
            continue
        before = _current_control_value(estore)
        target = before - min(headroom, remaining)
        lower = -max(0.0, _as_float(getattr(estore, "charge_p_max", headroom)))
        upper = max(0.0, _as_float(getattr(estore, "dis_charge_p_max", headroom)))
        before, after = _probe_set_p_ctrl(model, estore, target, "storage", lower, upper)
        absorbed = max(0.0, before - after)
        remaining -= absorbed
        logger.info(
            "储能 SOC 约束小步充电 %s p_ctrl %.3f -> %.3f soc=%.3f",
            getattr(estore, "name", getattr(estore, "idx", "")),
            before,
            after,
            _as_float(getattr(estore, "soc_cur", 0.0)),
        )
    return remaining


def _probe_storage_discharge(logger, model, deficit: float) -> float:
    remaining = max(0.0, deficit)
    for estore in _storage_by_soc(model, reverse=True):
        if remaining <= DEFAULT_BALANCE_DEADBAND:
            break
        headroom = _storage_soc_power_limit(model, estore, charging=False)
        if headroom <= 0.0:
            continue
        before = _current_control_value(estore)
        target = before + min(headroom, remaining)
        lower = -max(0.0, _as_float(getattr(estore, "charge_p_max", headroom)))
        upper = max(0.0, _as_float(getattr(estore, "dis_charge_p_max", headroom)))
        before, after = _probe_set_p_ctrl(model, estore, target, "storage", lower, upper)
        supplied = max(0.0, after - before)
        remaining -= supplied
        logger.info(
            "储能 SOC 约束小步放电 %s p_ctrl %.3f -> %.3f soc=%.3f",
            getattr(estore, "name", getattr(estore, "idx", "")),
            before,
            after,
            _as_float(getattr(estore, "soc_cur", 0.0)),
        )
    return remaining


def _probe_diesel(logger, model, mismatch: float) -> float:
    """Use thermal/柴油机 only for the residual after renewable/storage trials."""

    remaining = max(0.0, mismatch)
    for gen in _agc_resource_groups(model)["diesel"]:
        if remaining <= DEFAULT_BALANCE_DEADBAND:
            break
        if not _is_running(gen):
            continue
        lower = max(0.0, _as_float(getattr(gen, "p_min", 0.0)))
        upper = max(lower, _as_float(getattr(gen, "p_max", 0.0)))
        before, after = _probe_set_p_ctrl(model, gen, min(upper, before := _current_control_value(gen) + remaining), "diesel", lower, upper)
        supplied = max(0.0, after - before)
        remaining -= supplied
        logger.info("柴发残差小步补缺 %s p_ctrl %.3f -> %.3f", getattr(gen, "name", getattr(gen, "idx", "")), before, after)
    return remaining


def _probe_reduce_diesel(logger, model, surplus: float) -> float:
    remaining = max(0.0, surplus)
    for gen in _agc_resource_groups(model)["diesel"]:
        if remaining <= DEFAULT_BALANCE_DEADBAND:
            break
        if not _is_running(gen):
            continue
        lower = max(0.0, _as_float(getattr(gen, "p_min", 0.0)))
        upper = max(lower, _as_float(getattr(gen, "p_max", 0.0)))
        before, after = _probe_set_p_ctrl(model, gen, max(lower, _current_control_value(gen) - remaining), "diesel", lower, upper)
        reduced = max(0.0, before - after)
        remaining -= reduced
        logger.info("柴发优先小步降出力 %s p_ctrl %.3f -> %.3f", getattr(gen, "name", getattr(gen, "idx", "")), before, after)
    return remaining


def do_wind_solar_priority_ctrl(logger, model) -> None:
    """新能源优先的实测小步试探控制。

    ``p_fur`` is intentionally absent from this decision path.  Each cycle
    first computes the renewable acceptance space from measured load/output,
    diesel minimum output, and SOC-limited storage charging room.  Renewable
    units then receive one small trial step.  Storage and diesel only cover the
    residual, while surplus is absorbed by storage before renewable curtailment.
    """

    groups = _agc_resource_groups(model)
    load_target = _load_target(model)
    acceptance = calculate_renewable_acceptance_space(model)
    logger.info(
        "AGC 新能源优先试探开始: load=%.3f renewable_measured=%.3f acceptance=%.3f storage_charge=%.3f",
        load_target,
        acceptance["renewable_actual"],
        acceptance["acceptance_space"],
        acceptance["storage_charge_space"],
    )

    _probe_dispatch_renewables(logger, model, acceptance["acceptance_space"])
    mismatch = _controlled_power_mismatch(model)
    if mismatch > DEFAULT_BALANCE_DEADBAND:
        logger.info("新能源试探后仍有缺额 %.3f，按储能->燃料电池->柴发小步补偿", mismatch)
        mismatch = _probe_storage_discharge(logger, model, mismatch)
        mismatch = _probe_fuel_cell_support(logger, model, mismatch)
        mismatch = _probe_diesel(logger, model, mismatch)
    elif mismatch < -DEFAULT_BALANCE_DEADBAND:
        surplus = -mismatch
        logger.info("新能源试探后存在富余 %.3f，按降柴->储能->制氢->弃电消纳", surplus)
        surplus = _probe_reduce_diesel(logger, model, surplus)
        surplus = _probe_storage_charge(logger, model, surplus)
        surplus = _probe_hydrogen_absorb(logger, model, surplus)
        surplus = _curtail_renewable(logger, model, surplus)

    model.agc_balance_mismatch = _controlled_power_mismatch(model)
    model.agc_control_mode = "measured_probe_renewable_priority"
    logger.info("AGC 新能源优先试探结束: mismatch=%.3f", model.agc_balance_mismatch)


def agc_ctrl_stg_check_default(logger, model) -> None:
    mismatch = _as_float(getattr(model, "agc_balance_mismatch", 0.0))
    if abs(mismatch) > DEFAULT_BALANCE_DEADBAND:
        logger.warning("AGC 策略校验: 仍有功率缺额 %.3f", mismatch)
    else:
        logger.info("AGC 策略校验: 功率平衡满足死区")


def _update_yc(config: SystemConfig) -> None:
    client = config.fes_client
    for yc in getattr(config.model, "yc", []):
        result, _name, value = client.get_yc_value(client.schema, client.context, yc.rtu, yc.pnt)
        if result:
            yc.value = value
        else:
            config.logger.info("无法更新遥测数据 %s rtu=%s pnt=%s，使用本地值 %s", yc.name, yc.rtu, yc.pnt, yc.value)


def _update_yx(config: SystemConfig) -> None:
    client = config.fes_client
    for yx in getattr(config.model, "yx", []):
        result, _name, value = client.get_yx_value(client.schema, client.context, yx.rtu, yx.pnt)
        if result:
            yx.value = value
        else:
            config.logger.info("无法更新遥信数据 %s rtu=%s pnt=%s", yx.name, yx.rtu, yx.pnt)


def _log_power_group(logger: logging.Logger, title: str, devices: Iterable, attr_name: str = "p_cur") -> None:
    logger.info("采集<%s>设备实时信息", title)
    for device in devices:
        value = _telemetry_value(
            getattr(device, "p_yc", None),
            _power_value(device, attr_name, "p", "p_set", default=0.0),
        )
        setattr(device, attr_name, value)
        logger.info("    %s.有功: %.2f", getattr(device, "name", getattr(device, "idx", "")), value)


def data_gather(config: SystemConfig) -> None:
    """Gather telemetry and refresh model runtime fields."""
    _update_yc(config)
    _update_yx(config)
    model = config.model
    groups = _agc_resource_groups(model)
    _log_power_group(config.logger, "柴发/常规机组", groups["diesel"])
    _log_power_group(config.logger, "新能源（AC/DC）", groups["renewable"])

    config.logger.info("采集<负荷>设备实时信息")
    for load in _all_load_devices(model):
        measured = _telemetry_value(
            getattr(load, "p_yc", None),
            _power_value(load, "p", "pbase", "p_set", default=0.0),
        )
        load.p_cur = measured
        # Keep the legacy field populated for file compatibility, but never
        # consume it in the default AGC strategy.
        if hasattr(load, "p_fur"):
            load.p_fur = measured
        load.p_add = 0.0
        config.logger.info("    %s.有功: %.2f", getattr(load, "name", getattr(load, "idx", "")), load.p_cur)

    config.logger.info("采集<储能>设备实时信息")
    for estore in groups["storage"]:
        estore.p_cur = _telemetry_value(getattr(estore, "p_yc", None), _as_float(getattr(estore, "p_cur", 0.0)))
        estore.soc_cur = _telemetry_value(getattr(estore, "soc_yc", None), _as_float(getattr(estore, "soc_cur", 0.5)))
        config.logger.info("    %s.有功: %.2f", getattr(estore, "name", getattr(estore, "idx", "")), estore.p_cur)
        config.logger.info("    %s.soc: %.2f", getattr(estore, "name", getattr(estore, "idx", "")), estore.soc_cur)

    config.logger.info("采集天气信息:")
    config.logger.info("    风速:%.3fm/s", _telemetry_value(getattr(model, "wind_speed", None)))
    config.logger.info("    辐照:%.3fW/m^2", _telemetry_value(getattr(model, "solor_irrad", None)))
    config.logger.info("    气温:%.3f摄氏度", _telemetry_value(getattr(model, "env_temp", None)))


def _wait_until_next_period(period_s: int) -> None:
    now = time.time()
    sleep_s = period_s - (now % period_s)
    if sleep_s < period_s:
        time.sleep(max(0.0, sleep_s))


def agc_control_step(
    config: SystemConfig,
    do_one_ctrl: Callable,
    agc_ctrl_stg_check: Callable,
    do_renew_gen_fur_calc: Callable,
) -> None:
    """Run one gather/forecast/control/check/export cycle without periodic waiting."""
    config.logger.info("[单步采集 %s]", config.gather_count)
    data_gather(config)
    do_renew_gen_fur_calc(
        config.logger,
        config.model,
        _telemetry_value(getattr(config.model, "wind_speed", None)),
        _telemetry_value(getattr(config.model, "solor_irrad", None)),
        _telemetry_value(getattr(config.model, "env_temp", None)),
    )
    config.gather_count += 1
    config.logger.info("[单步控制 %s]", config.control_count)
    do_one_ctrl(config.logger, config.model)
    agc_ctrl_stg_check(config.logger, config.model)
    if config.ctrl_output_file is not None:
        export_control_results(config.model, config.ctrl_output_file)
        config.logger.info("控制结果已输出: %s", config.ctrl_output_file)
    config.control_count += 1
    _record_database_cycle(config)


def agc_control_once(config: SystemConfig, do_one_ctrl: Callable, agc_ctrl_stg_check: Callable) -> None:
    """Run one control/check/export cycle without gather, forecast, loop waiting."""
    config.logger.info("[单步控制 %s]", config.control_count)
    do_one_ctrl(config.logger, config.model)
    agc_ctrl_stg_check(config.logger, config.model)
    if config.ctrl_output_file is not None:
        export_control_results(config.model, config.ctrl_output_file)
        config.logger.info("控制结果已输出: %s", config.ctrl_output_file)
    config.control_count += 1
    _record_database_cycle(config)


def _record_database_cycle(config: SystemConfig) -> None:
    """Persist one completed calculation cycle when SQLite is available."""
    store = getattr(config, "data_store", None)
    if store is None:
        return
    try:
        store.record_cycle(config.model, config)
    except Exception:
        config.logger.exception("AGC SQLite 周期写入失败")


def agc_control_loop(config: SystemConfig, max_control_cycles: Optional[int] = None) -> None:
    """Run periodic data gathering and AGC decisions."""
    do_one_ctrl, agc_ctrl_stg_check, do_renew_gen_fur_calc = _load_control_functions()
    config.logger.info("=" * 60)
    config.logger.info("风-光-储-柴-荷 联合控制系统启动")
    config.logger.info("控制周期: %s秒, 采集周期: %s秒", config.ctrl_trigger, config.gather_trigger)
    config.logger.info("-" * 60)
    _wait_until_next_period(config.ctrl_trigger)

    next_gather = time.monotonic()
    next_control = time.monotonic()
    try:
        while config.is_running:
            now = time.monotonic()
            if now >= next_gather:
                config.logger.info("[采集周期 %s]", config.gather_count)
                data_gather(config)
                do_renew_gen_fur_calc(
                    config.logger,
                    config.model,
                    _telemetry_value(getattr(config.model, "wind_speed", None)),
                    _telemetry_value(getattr(config.model, "solor_irrad", None)),
                    _telemetry_value(getattr(config.model, "env_temp", None)),
                )
                config.gather_count += 1
                next_gather += config.gather_trigger
                config.logger.info("-" * 60)

            if now >= next_control:
                config.logger.info("[控制周期 %s]", config.control_count)
                do_one_ctrl(config.logger, config.model)
                agc_ctrl_stg_check(config.logger, config.model)
                if config.ctrl_output_file is not None:
                    export_control_results(config.model, config.ctrl_output_file)
                    config.logger.info("控制结果已输出: %s", config.ctrl_output_file)
                config.control_count += 1
                _record_database_cycle(config)
                next_control += config.ctrl_trigger
                config.logger.info("-" * 60)
                if max_control_cycles is not None and config.control_count >= max_control_cycles:
                    config.is_running = False

            sleep_to = min(next_gather, next_control) - time.monotonic()
            time.sleep(max(0.05, min(0.5, sleep_to)))
    except KeyboardInterrupt:
        config.logger.info("程序被用户中断")
    finally:
        config.logger.info("最终统计:")
        config.logger.info("  总共执行了 %s 个控制周期", config.control_count)
        config.logger.info("  总共采集了 %s 次数据", config.gather_count)


def load_model(model_file: Path):
    return efile_factory(EBook(model_file).to_dict())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AGC control loop for wind/solar/storage/diesel/load coordination.")
    parser.add_argument("--model", default=str(DEFAULT_MODEL_FILE), help="AGC model E file.")
    parser.add_argument("--log-file", default=None, help="Log file path.")
    parser.add_argument("--ctrl-output", default=str(DEFAULT_CTRL_OUTPUT_FILE), help="Control result E file path. Use empty string to disable.")
    parser.add_argument("--ctrl-trigger", type=int, default=None, help="Control period in seconds. Default uses E file or 60.")
    parser.add_argument("--gather-trigger", type=int, default=None, help="Gather period in seconds. Default uses E file or 10.")
    parser.add_argument("--max-control-cycles", type=int, default=None, help="Stop after N control cycles; useful for tests.")
    parser.add_argument("--step", action="store_true", help="Run one gather/forecast/control/check cycle immediately and exit.")
    parser.add_argument("--ctrl-step", action="store_true", help="Run one control/check cycle immediately and exit; no gather or forecast.")
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    model_file = Path(args.model)
    log_file = None if args.log_file is None else Path(args.log_file)
    ctrl_output_file = None if args.ctrl_output == "" else Path(args.ctrl_output)
    print(f"日志文件: {log_file or _default_log_file()}")
    print("系统启动中...")
    model = load_model(model_file)
    config = initialize_system(
        model,
        log_file=log_file,
        ctrl_output_file=ctrl_output_file,
        ctrl_trigger=args.ctrl_trigger,
        gather_trigger=args.gather_trigger,
    )
    if args.ctrl_step:
        do_one_ctrl, agc_ctrl_stg_check, _ = _load_control_functions()
        agc_control_once(config, do_one_ctrl, agc_ctrl_stg_check)
    elif args.step:
        do_one_ctrl, agc_ctrl_stg_check, do_renew_gen_fur_calc = _load_control_functions()
        agc_control_step(config, do_one_ctrl, agc_ctrl_stg_check, do_renew_gen_fur_calc)
    else:
        agc_control_loop(config, max_control_cycles=args.max_control_cycles)
    config.logger.info("系统运行结束")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
