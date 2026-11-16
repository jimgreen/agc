"""Deterministic AGC stress test and HTML report generator.

The test intentionally exercises the default measured renewable-priority
controller directly, without opening the runtime SQLite store.  Each case is
created from a fresh deep copy of ``model_real.e`` so no case can contaminate
the next one.  The report separates device constraints, system balance
progress, renewable-priority evidence, and residual imbalance caused by
one-cycle probe limits or insufficient resources.
"""

from __future__ import annotations

import copy
import html
import logging
import math
import random
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


AGC_DIR = Path(__file__).resolve().parents[1]
if str(AGC_DIR) not in sys.path:
    sys.path.insert(0, str(AGC_DIR))

import agc_ctrl
from efile_read import efile_factory_from_file


DEFAULT_CASES = 100000
DEFAULT_SEED = 20261117
EPS = 1e-7
BALANCE_DEADBAND = agc_ctrl.DEFAULT_BALANCE_DEADBAND


class DummyConfig:
    """Minimal command sink used by the controller's yt probes."""

    def __init__(self) -> None:
        self.commands: List[Tuple[str, float]] = []
        self.events: List[Tuple] = []

    def send_yt_cmd(self, yt, value: float) -> None:
        value = float(value)
        yt.ctrl_value = value
        yt.p_ctrl = value
        self.commands.append((str(getattr(yt, "name", getattr(yt, "id", ""))), value))
        self.events.append(("yt", str(getattr(yt, "control_role", "")).upper(), yt, value))


@dataclass
class DeviceBefore:
    kind: str
    device: object
    p_ctrl: float
    p_cur: float
    soc: float | None
    state: str


@dataclass
class CaseResult:
    case_id: int
    scenario: str
    initial_mismatch: float = 0.0
    final_mismatch: float = 0.0
    measured_initial_mismatch: float = 0.0
    measured_final_mismatch: float = 0.0
    acceptance_space: float = 0.0
    renewable_headroom: float = 0.0
    renewable_increase: float = 0.0
    renewable_decrease: float = 0.0
    storage_discharge: float = 0.0
    storage_charge: float = 0.0
    diesel_increase: float = 0.0
    diesel_decrease: float = 0.0
    hydrogen_increase: float = 0.0
    fuel_cell_increase: float = 0.0
    device_safe: bool = True
    balance_non_worsening: bool = True
    system_safe: bool = True
    exact_balance: bool = False
    renewable_priority: bool = True
    priority_order: bool = True
    renewable_uncertainty_safe: bool = True
    resource_limited: bool = False
    exception: str = ""
    reasons: List[str] = field(default_factory=list)
    details: Dict[str, object] = field(default_factory=dict)


def _logger() -> logging.Logger:
    logger = logging.getLogger("agc-stress-test")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    logger.setLevel(logging.CRITICAL)
    return logger


def _f(value, default: float = 0.0) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _running(device) -> bool:
    return int(getattr(device, "run_stat", 1) or 0) == 1


def _state(device) -> str:
    value = str(getattr(device, "agc_control_state", "remote") or "remote").strip().lower()
    return "local" if value in {"local", "本地", "l"} else "remote"


def _groups(model) -> Dict[str, List[object]]:
    return agc_ctrl._agc_resource_groups(model)


def _all_devices(model) -> Iterable[Tuple[str, object]]:
    groups = _groups(model)
    for kind in ("renewable", "storage", "diesel"):
        for device in groups[kind]:
            yield kind, device


def _set_measurements(model, rng: random.Random, scenario: str) -> None:
    """Populate a case with safe initial values and local preset curves."""

    groups = _groups(model)

    # Loads are measured values.  The initial SCADA state is required to be
    # balanced.  The scenario still varies resource mix, operating state and
    # SOC; the load is normalized to the resulting measured generation at the
    # end of this function.
    load_modes = {
        "balanced": (0.25, 0.50),
        "deficit": (0.70, 1.00),
        "surplus": (0.05, 0.25),
        "renewable_rich": (0.25, 0.60),
        "low_soc": (0.60, 0.95),
        "high_soc": (0.05, 0.35),
        "local_mix": (0.30, 0.80),
        "stopped": (0.20, 0.85),
        "no_storage": (0.50, 0.95),
        "no_diesel": (0.50, 0.95),
        "no_renewable": (0.40, 0.90),
        "edge": (0.0, 1.0),
        "random": (0.0, 1.0),
    }
    low, high = load_modes[scenario]
    load_max = 1050.0
    target_load = rng.uniform(low * load_max, high * load_max)
    loads = [load for load in agc_ctrl._all_load_devices(model) if _running(load)]
    weights = [rng.uniform(0.2, 1.8) for _ in loads]
    total_weight = sum(weights) or 1.0
    for load, weight in zip(loads, weights):
        load.p_cur = target_load * weight / total_weight
        if hasattr(load, "p_set"):
            load.p_set = load.p_cur
    for load in agc_ctrl._all_load_devices(model):
        if not _running(load):
            load.p_cur = 0.0

    # Renewable measured output.  Local resources use a preset curve and are
    # excluded from automatic decisions; remote resources start at p_cur.
    renew_fraction = {
        "balanced": (0.05, 0.35),
        "deficit": (0.0, 0.35),
        "surplus": (0.45, 1.0),
        "renewable_rich": (0.70, 1.0),
        "low_soc": (0.10, 0.45),
        "high_soc": (0.55, 1.0),
        "local_mix": (0.10, 0.85),
        "stopped": (0.0, 0.9),
        "no_storage": (0.2, 0.9),
        "no_diesel": (0.2, 0.9),
        "no_renewable": (0.0, 0.0),
        "edge": (0.0, 1.0),
        "random": (0.0, 1.0),
    }[scenario]
    for gen in groups["renewable"]:
        if scenario == "no_renewable":
            gen.run_stat = 0
        if not _running(gen):
            gen.p_cur = 0.0
            gen.p_ctrl = 0.0
            continue
        p_max = max(0.0, _f(getattr(gen, "p_max", 0.0)))
        p_min = max(0.0, _f(getattr(gen, "p_min", 0.0)))
        fraction = rng.uniform(*renew_fraction)
        current = p_min + (p_max - p_min) * fraction
        gen.p_cur = current
        gen.p_ctrl = current
        if scenario == "local_mix" and rng.random() < 0.45:
            gen.agc_control_state = "local"
            # The preset curve is deliberately different from the random
            # measured value while remaining within the equipment envelope.
            gen.p_ctrl = _f(getattr(gen, "p_set", current), current)
            gen.p_ctrl = max(p_min, min(p_max, gen.p_ctrl))
            gen.p_cur = gen.p_ctrl
        elif rng.random() < 0.18:
            gen.agc_control_state = "local"
            gen.p_ctrl = current

    # Conventional generation.  Running units begin no lower than p_min.
    for gen in groups["diesel"]:
        if scenario in {"no_diesel", "edge"} and (scenario == "no_diesel" or rng.random() < 0.45):
            gen.run_stat = 0
        if not _running(gen):
            gen.p_cur = 0.0
            gen.p_ctrl = 0.0
            continue
        p_min = max(0.0, _f(getattr(gen, "p_min", 0.0)))
        p_max = max(p_min, _f(getattr(gen, "p_max", p_min)))
        if scenario == "surplus":
            fraction = rng.uniform(0.65, 1.0)
        elif scenario == "deficit":
            fraction = rng.uniform(0.0, 0.45)
        else:
            fraction = rng.uniform(0.0, 1.0)
        current = p_min + (p_max - p_min) * fraction
        gen.p_cur = current
        gen.p_ctrl = current
        if scenario == "local_mix" and rng.random() < 0.40:
            gen.agc_control_state = "local"
        elif rng.random() < 0.12:
            gen.agc_control_state = "local"
        if _state(gen) == "local":
            gen.p_cur = gen.p_ctrl

    # Storage starts with zero active-power command.  SOC is deliberately
    # sampled around the limits in dedicated scenarios; this stresses both
    # charging and discharging protections.
    for estore in groups["storage"]:
        if scenario == "no_storage":
            estore.run_stat = 0
        if not _running(estore):
            estore.p_cur = 0.0
            estore.p_ctrl = 0.0
            continue
        soc_min = _f(getattr(estore, "soc_min", 0.0))
        soc_max = max(soc_min, _f(getattr(estore, "soc_max", 1.0)))
        if scenario == "low_soc":
            soc = rng.uniform(soc_min, soc_min + 0.04 * (soc_max - soc_min))
        elif scenario == "high_soc":
            soc = rng.uniform(soc_max - 0.04 * (soc_max - soc_min), soc_max)
        elif scenario == "edge":
            soc = soc_min if rng.random() < 0.5 else soc_max
        else:
            soc = rng.uniform(soc_min, soc_max)
        estore.soc_cur = soc
        estore.p_cur = 0.0
        estore.p_ctrl = 0.0
        if scenario == "local_mix" and rng.random() < 0.35:
            estore.agc_control_state = "local"
            # A local default curve may charge or discharge, but never starts
            # at a SOC boundary where that command would be unsafe.
            usable = soc_max - soc_min
            if soc <= soc_min + 0.02 * usable or soc >= soc_max - 0.02 * usable:
                estore.p_ctrl = 0.0
            else:
                charge = _f(getattr(estore, "charge_p_max", 0.0))
                discharge = _f(getattr(estore, "dis_charge_p_max", 0.0))
                estore.p_ctrl = rng.uniform(-0.5 * charge, 0.5 * discharge)
            estore.p_cur = estore.p_ctrl

    # A few cases exercise different device step/deadband settings without
    # ever creating invalid initial setpoints.
    for kind, device in _all_devices(model):
        if rng.random() < 0.12:
            device.p_step = rng.choice([0.0, 0.25, 1.0, 2.5, 5.0, 10.0])
            # Production normalization prefers agc_control_step when it is
            # already present, so keep the randomized model field and the
            # normalized runtime field consistent.
            device.agc_control_step = device.p_step
        if rng.random() < 0.12:
            device.p_dead = rng.choice([0.0, 0.1, 0.5, 1.0, 2.0])
            device.agc_control_deadband = device.p_dead

    for yt in getattr(model, "yt", []) or []:
        yt.ctrl_value = 0.0
        yt.p_ctrl = 0.0

    # SCADA cases enter the controller in balance.  Match the measured load to
    # measured generation after all resource permutations have been applied.
    # A charging-only isolated storage fleet cannot serve a positive load, so
    # reset that local preset to its neutral value before balancing.
    measured_generation = sum(
        _f(getattr(device, "p_cur", getattr(device, "p_ctrl", 0.0)))
        for _kind, device in _all_devices(model)
        if _running(device)
    )
    if measured_generation < 0.0:
        for device in groups["storage"]:
            if _running(device) and _f(getattr(device, "p_cur", 0.0)) < 0.0:
                device.p_cur = 0.0
                device.p_ctrl = 0.0
        measured_generation = sum(
            _f(getattr(device, "p_cur", getattr(device, "p_ctrl", 0.0)))
            for _kind, device in _all_devices(model)
            if _running(device)
        )
    measured_generation = max(0.0, measured_generation)
    loads = [load for load in agc_ctrl._all_load_devices(model) if _running(load)]
    weights = [rng.uniform(0.2, 1.8) for _ in loads]
    total_weight = sum(weights) or 1.0
    for load, weight in zip(loads, weights):
        load.p_cur = measured_generation * weight / total_weight
        if hasattr(load, "p_set"):
            load.p_set = load.p_cur
    for load in agc_ctrl._all_load_devices(model):
        if not _running(load):
            load.p_cur = 0.0


def _prepare_case(base_model, rng: random.Random, scenario: str):
    model = copy.deepcopy(base_model)
    # Do not inherit any cached groups/runtime state if a future model adds it.
    for name in ("_agc_resource_groups", "_para_by_name", "config"):
        if hasattr(model, name):
            try:
                delattr(model, name)
            except AttributeError:
                pass
    model.config = DummyConfig()
    _set_measurements(model, rng, scenario)
    groups = _groups(model)
    # Ensure p_ctrl is initialized after all test fields are set.  This is
    # also the same runtime normalization used by the production controller.
    for kind, device in _all_devices(model):
        if not hasattr(device, "p_ctrl") or getattr(device, "p_ctrl") is None:
            device.p_ctrl = _f(getattr(device, "p_cur", 0.0))
        agc_ctrl._ensure_control_limits(model, device, kind)
    return model, groups


def _snapshot(model) -> List[DeviceBefore]:
    out = []
    for kind, device in _all_devices(model):
        out.append(
            DeviceBefore(
                kind=kind,
                device=device,
                p_ctrl=_f(getattr(device, "p_ctrl", getattr(device, "p_cur", 0.0))),
                p_cur=_f(getattr(device, "p_cur", 0.0)),
                soc=_f(getattr(device, "soc_cur")) if hasattr(device, "soc_cur") else None,
                state=_state(device),
            )
        )
    return out


def _sum_delta(before: List[DeviceBefore], kind: str, positive: bool) -> float:
    total = 0.0
    for item in before:
        if item.kind != kind:
            continue
        delta = _f(getattr(item.device, "p_ctrl", 0.0)) - item.p_ctrl
        if positive:
            total += max(0.0, delta)
        else:
            total += max(0.0, -delta)
    return total


def _measured_power_mismatch(model) -> float:
    """Compute balance from telemetry-backed p_cur, excluding p_ctrl trials."""

    load = agc_ctrl._load_target(model)
    groups = _groups(model)
    generation = sum(
        _f(getattr(device, "p_cur", 0.0))
        for kind in ("renewable", "storage", "diesel")
        for device in groups[kind]
        if _running(device)
    )
    return load - generation


def _check_case(case_id: int, scenario: str, model, groups, before: List[DeviceBefore], initial_mismatch: float, acceptance: Dict[str, float], command_trace=None, measured_initial_mismatch: float = 0.0) -> CaseResult:
    result = CaseResult(
        case_id=case_id,
        scenario=scenario,
        initial_mismatch=initial_mismatch,
        measured_initial_mismatch=measured_initial_mismatch,
        acceptance_space=_f(acceptance.get("acceptance_space")),
    )
    result.final_mismatch = _f(agc_ctrl._controlled_power_mismatch(model))
    result.measured_final_mismatch = _measured_power_mismatch(model)
    result.renewable_increase = _sum_delta(before, "renewable", True)
    result.renewable_decrease = _sum_delta(before, "renewable", False)
    result.storage_discharge = _sum_delta(before, "storage", True)
    result.storage_charge = _sum_delta(before, "storage", False)
    result.diesel_increase = _sum_delta(before, "diesel", True)
    result.diesel_decrease = _sum_delta(before, "diesel", False)
    result.hydrogen_increase = sum(
        max(0.0, _f(getattr(yt, "ctrl_value", 0.0)))
        for yt in getattr(model, "yt", [])
        if str(getattr(yt, "control_role", "")).upper() == agc_ctrl.YT_ROLE_HYDROGEN_ABSORB
    )
    result.fuel_cell_increase = sum(
        max(0.0, _f(getattr(yt, "ctrl_value", 0.0)))
        for yt in getattr(model, "yt", [])
        if str(getattr(yt, "control_role", "")).upper() == agc_ctrl.YT_ROLE_FUEL_CELL_SUPPORT
    )
    result.renewable_headroom = sum(
        max(0.0, _f(getattr(item.device, "p_max", 0.0)) - item.p_ctrl)
        for item in before
        if item.kind == "renewable" and item.state == "remote" and _running(item.device)
    )

    # Device-level safety invariants.
    for item in before:
        device = item.device
        after = _f(getattr(device, "p_ctrl", 0.0), float("nan"))
        if not math.isfinite(after):
            result.reasons.append(f"{item.kind}:{getattr(device, 'name', '')} 指令非有限")
            continue
        if item.kind == "storage":
            lower = -max(0.0, _f(getattr(device, "charge_p_max", 0.0)))
            upper = max(0.0, _f(getattr(device, "dis_charge_p_max", 0.0)))
        else:
            # A stopped generator is safely represented by zero command even
            # when its nameplate p_min is non-zero.
            lower = 0.0 if not _running(device) else max(0.0, _f(getattr(device, "p_min", 0.0)))
            upper = max(lower, _f(getattr(device, "p_max", lower)))
        if after < lower - EPS or after > upper + EPS:
            result.reasons.append(f"{item.kind}:{getattr(device, 'name', '')} 越过功率边界")
        if not _running(device) and abs(after) > EPS:
            result.reasons.append(f"{item.kind}:{getattr(device, 'name', '')} 停运仍有指令")
        if item.state == "local" and abs(after - item.p_ctrl) > EPS:
            result.reasons.append(f"{item.kind}:{getattr(device, 'name', '')} 本地指令被自动改变")
        if item.state == "remote" and _running(device):
            step = max(0.0, _f(getattr(device, "p_step", getattr(device, "agc_control_step", 0.0))))
            if abs(after - item.p_ctrl) > step + EPS:
                result.reasons.append(f"{item.kind}:{getattr(device, 'name', '')} 超过单周期步长")
            # Deadband is checked against every actual setter transition.  A
            # controller may legitimately make a large probe and later undo
            # part of it when the balance sign changes; checking only the net
            # delta would falsely report that as a deadband violation.
        if item.kind == "diesel" and _running(device):
            p_min = max(0.0, _f(getattr(device, "p_min", 0.0)))
            if after < p_min - EPS:
                result.reasons.append(f"柴发:{getattr(device, 'name', '')} 低于最小稳定出力")
        if item.kind == "storage" and item.soc is not None and _running(device):
            soc = item.soc
            soc_min = _f(getattr(device, "soc_min", 0.0))
            soc_max = max(soc_min, _f(getattr(device, "soc_max", 1.0)))
            dead = max(0.0, _f(getattr(device, "soc_dead", 0.0)))
            if soc <= soc_min + dead + EPS and after > EPS:
                result.reasons.append(f"储能:{getattr(device, 'name', '')} SOC下限仍放电")
            if soc >= soc_max - dead - EPS and after < -EPS:
                result.reasons.append(f"储能:{getattr(device, 'name', '')} SOC上限仍充电")
    result.device_safe = not result.reasons

    # A safe controller must not make the measured power mismatch worse.  A
    # one-cycle residual is expected because the production policy is bounded
    # by p_step; it is reported separately from this safety invariant.
    result.balance_non_worsening = abs(result.measured_final_mismatch) <= abs(result.measured_initial_mismatch) + 1e-5
    if not result.balance_non_worsening:
        result.reasons.append("功率不平衡幅值变差")
    result.system_safe = result.device_safe and result.balance_non_worsening and not result.exception
    result.exact_balance = abs(result.final_mismatch) <= BALANCE_DEADBAND

    # Renewable priority is only judged when a remote running renewable has
    # room and the calculated acceptance space exceeds the active deadband.
    priority_capability = sum(
        min(
            max(0.0, _f(getattr(item.device, "p_max", 0.0)) - item.p_ctrl),
            max(0.0, _f(getattr(item.device, "agc_control_step", getattr(item.device, "p_step", 0.0)))),
        )
        for item in before
        if item.kind == "renewable" and item.state == "remote" and _running(item.device)
    )
    priority_need = min(result.acceptance_space, priority_capability)
    # When the measured system is already in surplus, renewable curtailment
    # and absorption are the correct priority evidence; increasing renewable
    # output is not required merely because acceptance-space arithmetic has
    # residual room.
    if initial_mismatch < -BALANCE_DEADBAND:
        priority_need = 0.0
    result.renewable_priority = not (priority_need > 0.5 and result.renewable_increase <= EPS)
    if not result.renewable_priority:
        result.reasons.append("存在可接纳新能源空间但未先增加远方新能源")

    # Audit the actual dispatch order.  A resource category that did not move
    # in a case is not a violation; whenever two categories did move, their
    # first effective transitions must follow the documented priority chain.
    events = list(getattr(getattr(model, "config", None), "events", []) or [])
    effective = []
    for event in events:
        if event[0] == "yt":
            role = event[1]
            if role == agc_ctrl.YT_ROLE_HYDROGEN_ABSORB and event[3] > EPS:
                effective.append("hydrogen")
            elif role == agc_ctrl.YT_ROLE_FUEL_CELL_SUPPORT and event[3] > EPS:
                effective.append("fuel_cell")
            continue
        _, kind, device, before_value, after_value, _deadband = event
        delta = after_value - before_value
        if abs(delta) <= EPS:
            continue
        if kind == "renew":
            effective.append("renewable_up" if delta > 0 else "renewable_down")
        elif kind == "storage":
            effective.append("storage_discharge" if delta > 0 else "storage_charge")
        elif kind == "diesel":
            effective.append("diesel_up" if delta > 0 else "diesel_down")
    if initial_mismatch > BALANCE_DEADBAND:
        desired = ["renewable_up", "storage_discharge", "fuel_cell", "diesel_up"]
    elif initial_mismatch < -BALANCE_DEADBAND:
        desired = ["diesel_down", "storage_charge", "hydrogen", "renewable_down"]
    else:
        desired = []
    positions = {name: min((i for i, value in enumerate(effective) if value == name), default=10**9) for name in desired}
    moved = [name for name in desired if positions[name] < 10**9]
    if any(positions[moved[i]] > positions[moved[i + 1]] for i in range(len(moved) - 1)):
        result.priority_order = False
        result.reasons.append("新能源优先调度顺序不符合策略")

    # Validate the deadband on each setter call when a trace is available.
    trace_violation = False
    for kind, device, before_value, after_value, deadband in command_trace or []:
        if abs(after_value - before_value) > EPS and abs(after_value - before_value) <= deadband + EPS:
            trace_violation = True
            result.reasons.append(f"{kind}:{getattr(device, 'name', '')} 违反死区")

    # Trace findings are device-level safety findings too.  Recompute the
    # aggregate flags after adding them; otherwise the detail table would show
    # deadband failures while the headline incorrectly reported a pass.
    result.device_safe = result.device_safe and not trace_violation
    if abs(result.measured_initial_mismatch) <= BALANCE_DEADBAND and result.renewable_increase > EPS:
        if result.diesel_decrease > EPS or result.storage_charge > EPS:
            result.renewable_uncertainty_safe = False
            result.reasons.append("新能源未确认上涨却降低柴发或增加储能充电")
    result.system_safe = result.device_safe and result.balance_non_worsening and result.renewable_uncertainty_safe and not result.exception

    # Residual imbalance is expected when a single probe step cannot close the
    # mismatch, or when all remaining resources are at a hard boundary.
    result.resource_limited = (
        abs(result.final_mismatch) > BALANCE_DEADBAND
        and abs(result.final_mismatch) < abs(initial_mismatch) + 1e-5
    )
    result.details = {
        "renewable_headroom": round(result.renewable_headroom, 6),
        "acceptance_space": round(result.acceptance_space, 6),
        "initial_mismatch": round(initial_mismatch, 6),
        "final_mismatch": round(result.final_mismatch, 6),
        "measured_initial_mismatch": round(result.measured_initial_mismatch, 6),
        "measured_final_mismatch": round(result.measured_final_mismatch, 6),
    }
    return result


def run_case(case_id: int, base_model, rng: random.Random, scenario: str, logger: logging.Logger) -> CaseResult:
    model = None
    try:
        model, groups = _prepare_case(base_model, rng, scenario)
        before = _snapshot(model)
        initial_mismatch = _f(agc_ctrl._controlled_power_mismatch(model))
        measured_initial_mismatch = _measured_power_mismatch(model)
        acceptance = agc_ctrl.calculate_renewable_acceptance_space(model)
        trace = []
        original_set_p_ctrl = agc_ctrl._set_p_ctrl

        def traced_set_p_ctrl(local_model, device, target, kind, lower, upper):
            before_value = agc_ctrl._current_control_value(device)
            deadband = agc_ctrl._control_deadband(local_model, device, kind)
            output = original_set_p_ctrl(local_model, device, target, kind, lower, upper)
            trace.append((kind, device, float(output[0]), float(output[1]), float(deadband)))
            model.config.events.append(("device", kind, device, float(output[0]), float(output[1]), float(deadband)))
            return output

        agc_ctrl._set_p_ctrl = traced_set_p_ctrl
        try:
            agc_ctrl.do_wind_solar_priority_ctrl(logger, model)
        finally:
            agc_ctrl._set_p_ctrl = original_set_p_ctrl
        result = _check_case(case_id, scenario, model, groups, before, initial_mismatch, acceptance, trace, measured_initial_mismatch)
        if result.resource_limited:
            result.details["residual_class"] = "步长或可用资源限制"
        elif result.exact_balance:
            result.details["residual_class"] = "已在死区内平衡"
        else:
            result.details["residual_class"] = "无改善或不可判定"
        return result
    except Exception as exc:  # A controller exception is a real test failure.
        result = CaseResult(case_id=case_id, scenario=scenario, exception=f"{type(exc).__name__}: {exc}")
        result.device_safe = result.system_safe = result.renewable_priority = False
        result.balance_non_worsening = False
        result.reasons = ["控制器异常"]
        return result


def _scenario_for(case_id: int, rng: random.Random) -> str:
    # Fixed strata guarantee coverage even if the random generator changes.
    strata = [
        "balanced", "deficit", "surplus", "renewable_rich", "low_soc",
        "high_soc", "local_mix", "stopped", "no_storage", "no_diesel",
        "no_renewable", "edge", "random",
    ]
    if case_id < len(strata) * 500:
        return strata[(case_id // 500) % len(strata)]
    return rng.choice(strata)


def _pct(part: int, total: int) -> str:
    return f"{(100.0 * part / total) if total else 0.0:.2f}%"


def _html_report(results: List[CaseResult], seed: int, elapsed: float, model_path: Path, output_path: Path) -> None:
    total = len(results)
    device_pass = sum(r.device_safe for r in results)
    system_pass = sum(r.system_safe for r in results)
    priority_pass = sum(r.renewable_priority and r.priority_order for r in results)
    balance_pass = sum(r.exact_balance for r in results)
    passive_balance_source = sum(
        r.storage_charge <= EPS and r.diesel_decrease <= EPS and r.fuel_cell_increase <= EPS and r.hydrogen_increase <= EPS
        for r in results
    )
    exceptions = [r for r in results if r.exception]
    resource_limited = sum(r.resource_limited for r in results)
    scenario_counts = Counter(r.scenario for r in results)
    scenario_stats = defaultdict(lambda: {"total": 0, "device": 0, "system": 0, "priority": 0, "balance": 0, "residual": 0})
    for r in results:
        stat = scenario_stats[r.scenario]
        stat["total"] += 1
        stat["device"] += int(r.device_safe)
        stat["system"] += int(r.system_safe)
        stat["priority"] += int(r.renewable_priority and r.priority_order)
        stat["balance"] += int(r.exact_balance)
        stat["residual"] += int(r.resource_limited)

    failed = [r for r in results if not r.system_safe or not r.renewable_priority or not r.priority_order or r.exception]
    worst = sorted(results, key=lambda r: (abs(r.final_mismatch), abs(r.initial_mismatch)), reverse=True)[:20]
    reason_counts = Counter(reason for r in results for reason in r.reasons)

    def esc(v) -> str:
        return html.escape(str(v))

    rows_scenario = "".join(
        "<tr>" + "".join(
            f"<td>{esc(value)}</td>" for value in (
                scenario,
                stat["total"],
                f"{stat['device']}/{stat['total']} ({_pct(stat['device'], stat['total'])})",
                f"{stat['system']}/{stat['total']} ({_pct(stat['system'], stat['total'])})",
                f"{stat['priority']}/{stat['total']} ({_pct(stat['priority'], stat['total'])})",
                f"{stat['balance']}/{stat['total']} ({_pct(stat['balance'], stat['total'])})",
                stat["residual"],
            )
        ) + "</tr>"
        for scenario, stat in sorted(scenario_stats.items())
    )
    rows_worst = "".join(
        "<tr>" + "".join(
            f"<td>{esc(value)}</td>" for value in (
                r.case_id,
                r.scenario,
                f"{r.initial_mismatch:.3f}",
                f"{r.final_mismatch:.3f}",
                f"{r.renewable_increase:.3f}",
                f"{r.storage_discharge:.3f}",
                f"{r.storage_charge:.3f}",
                f"{r.diesel_increase:.3f}",
                f"{r.diesel_decrease:.3f}",
                "通过" if r.system_safe else "失败",
                "; ".join(r.reasons[:2]) or "—",
            )
        ) + "</tr>"
        for r in worst
    )
    rows_failed = "".join(
        "<tr>" + "".join(
            f"<td>{esc(value)}</td>" for value in (
                r.case_id,
                r.scenario,
                "是" if r.device_safe else "否",
                "是" if r.system_safe else "否",
                "是" if r.renewable_priority and r.priority_order else "否",
                f"{r.initial_mismatch:.3f}",
                f"{r.final_mismatch:.3f}",
                r.exception or "；".join(r.reasons),
            )
        ) + "</tr>"
        for r in failed[:100]
    ) or '<tr><td colspan="8">没有控制器异常或安全/新能源优先判定失败的案例。</td></tr>'
    rows_reasons = "".join(
        f"<tr><td>{esc(reason)}</td><td>{count}</td><td>{_pct(count, total)}</td></tr>"
        for reason, count in reason_counts.most_common(20)
    ) or '<tr><td colspan="3">无失败原因。</td></tr>'

    conclusion = (
        "所有 SCADA 案例均在控制入口按实测功率平衡构造；本周期新能源指令上涨属于未确认试探，"
        f"设备安全通过率为 {_pct(device_pass, total)}，系统实测安全通过率为 {_pct(system_pass, total)}，"
        f"新能源优先策略通过率为 {_pct(priority_pass, total)}。"
        f"{passive_balance_source} 个案例保持平衡电源被动承担、未因未确认新能源上涨而反向降功率；"
        f"记录到 {resource_limited} 个需要后续遥测闭环观察的试探残差，精确处于控制死区的案例为 {balance_pass} 个。"
    )
    if exceptions:
        conclusion += f"发现 {len(exceptions)} 个控制器异常，需优先修复。"
    else:
        conclusion += "未发现控制器运行时异常。"

    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>AGC 大规模场景压力测试报告</title>
<style>
*{{box-sizing:border-box}} body{{margin:0;background:#f3f6fb;color:#182230;font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}}
.page{{max-width:1500px;margin:0 auto;padding:28px 32px 48px}} h1{{margin:0 0 8px;font-size:28px}} h2{{margin:28px 0 12px;font-size:19px}} h3{{margin:20px 0 8px;font-size:16px}}
.muted{{color:#66758a}} .lead{{background:#e9f4ff;border-left:4px solid #1677c8;border-radius:6px;padding:14px 16px;margin:18px 0}}
.grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}} .card{{background:#fff;border:1px solid #d9e2ec;border-radius:8px;padding:15px;box-shadow:0 1px 2px #13223812}} .metric{{font-size:26px;font-weight:700;margin:4px 0}} .label{{color:#64748b}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #d9e2ec;font-size:13px}} th,td{{padding:8px 9px;border-bottom:1px solid #e8edf3;text-align:left;vertical-align:top}} th{{background:#eef3f8;color:#334155;position:sticky;top:0}} tr:last-child td{{border-bottom:0}}
.ok{{color:#0b7a4b;font-weight:600}} .warn{{color:#a15c00;font-weight:600}} .bad{{color:#b42318;font-weight:600}} .note{{background:#fffaf0;border:1px solid #f1d49b;border-radius:7px;padding:12px 14px}}
code{{background:#edf2f7;border-radius:4px;padding:2px 4px}} .small{{font-size:12px}}
</style></head><body><main class="page">
<h1>AGC 大规模场景压力测试报告</h1>
<div class="muted">生成时间：{esc(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC'))}　|　模型：<code>{esc(model_path)}</code>　|　随机种子：<code>{seed}</code>　|　执行耗时：{elapsed:.2f} 秒</div>
<div class="lead">{esc(conclusion)}</div>
<div class="grid">
<div class="card"><div class="label">测试案例</div><div class="metric">{total:,}</div><div class="small">确定性随机 + 分层边界场景</div></div>
<div class="card"><div class="label">设备安全</div><div class="metric {'ok' if device_pass == total else 'warn'}">{device_pass:,} / {total:,}</div><div class="small">功率边界、步长、死区、SOC、本地/停运保护</div></div>
<div class="card"><div class="label">系统实测安全</div><div class="metric {'ok' if system_pass == total else 'warn'}">{system_pass:,} / {total:,}</div><div class="small">入口平衡、遥测平衡保持、无反向降平衡源</div></div>
<div class="card"><div class="label">新能源优先策略</div><div class="metric {'ok' if priority_pass == total else 'warn'}">{priority_pass:,} / {total:,}</div><div class="small">接纳空间、动作顺序和富余消纳链路</div></div>
</div>
<h2>判定口径</h2>
<p>设备安全检查每台风机、光伏、储能和柴发的指令是否有限、是否在功率边界内、是否遵守单周期步长和死区；停运设备不得带指令，远方自动控制不得改变本地设备，储能不得突破 SOC 方向约束，柴发不得低于最小稳定出力。</p>
<p>系统安全以 SCADA 实测功率检查：所有案例入口均为平衡，控制器只改变新能源的试探指令，保持 <code>p_cur</code> 遥测不变；不能把未确认的新能源指令上涨当作已经增加的功率。柴发、构网储能等平衡电源本周期不被反向降功率，后续由真实遥测闭环观察其被动承担的不平衡。</p>
<p>新能源优先检查：若计算出的接纳空间和远方运行新能源的可调上限均大于 0.5 MW，本周期必须出现新能源指令增加。动作顺序审计只检查实际发生的事件；本场景入口平衡，因此不人为制造缺额/富余链路。</p>
<h2>分场景统计</h2>
<p>场景标签表示新能源出力、储能 SOC、设备启停、远方/本地组合等压力维度；每个标签的 SCADA 入口仍先按实测总功率归一为平衡状态。</p>
<table><thead><tr><th>场景</th><th>案例数</th><th>设备安全</th><th>系统安全</th><th>新能源优先</th><th>精确平衡</th><th>试探指令残差</th></tr></thead><tbody>{rows_scenario}</tbody></table>
<h2>控制动作统计说明</h2>
<p>控制器每个案例只执行一个控制周期。SCADA 实测平衡时仅对远方新能源执行一个受边界、步长和死区限制的试探；不根据该试探指令去降低储能或柴发。表中控制指令变化是未确认试探的指令层信息，不能解释为实时功率失衡。</p>
<h2>试探指令变化最大的 20 个案例</h2>
<table><thead><tr><th>案例</th><th>场景</th><th>初始控制偏差</th><th>最终控制偏差</th><th>新能源增加</th><th>储能放电</th><th>储能充电</th><th>柴发增加</th><th>柴发减少</th><th>系统安全</th><th>原因</th></tr></thead><tbody>{rows_worst}</tbody></table>
<h2>失败/异常案例（最多 100 个）</h2>
<table><thead><tr><th>案例</th><th>场景</th><th>设备安全</th><th>系统安全</th><th>新能源优先</th><th>初始控制偏差</th><th>最终控制偏差</th><th>原因或异常</th></tr></thead><tbody>{rows_failed}</tbody></table>
<h2>判定原因汇总</h2>
<table><thead><tr><th>原因</th><th>次数</th><th>占全部案例</th></tr></thead><tbody>{rows_reasons}</tbody></table>
<h2>限制与工程结论</h2>
<div class="note"><p>{esc(conclusion)}</p><p>本测试使用模型级确定性随机仿真，验证了控制器代码可直接检查的边界和策略顺序，不能替代现场联调、真实遥测质量验证、通信中断/延迟测试、硬件保护、继电保护配合和实时数字仿真。报告中的“资源或步长限制”应结合现场控制周期、备用容量和设备爬坡能力进一步评估。</p></div>
<p class="muted small">输出文件：{esc(output_path)}</p>
</main></body></html>"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(document, encoding="utf-8")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run large deterministic AGC scenario stress tests and write HTML.")
    parser.add_argument("--cases", type=int, default=DEFAULT_CASES)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--model", type=Path, default=AGC_DIR / "model_real.e")
    parser.add_argument("--output", type=Path, default=AGC_DIR / "output" / "agc_stress_test_report.html")
    args = parser.parse_args()
    if args.cases < 10000:
        parser.error("--cases 必须不少于 10000")

    start = time.perf_counter()
    print(f"加载模型: {args.model}")
    base_model = efile_factory_from_file(args.model)
    logger = _logger()
    rng = random.Random(args.seed)
    results: List[CaseResult] = []
    for case_id in range(args.cases):
        scenario = _scenario_for(case_id, rng)
        results.append(run_case(case_id + 1, base_model, rng, scenario, logger))
        if (case_id + 1) % 1000 == 0 or case_id + 1 == args.cases:
            print(f"已完成 {case_id + 1}/{args.cases}")
    elapsed = time.perf_counter() - start
    _html_report(results, args.seed, elapsed, args.model, args.output)

    total = len(results)
    print(f"HTML 报告: {args.output}")
    print(f"案例数: {total}")
    print(f"设备安全: {sum(r.device_safe for r in results)}/{total}")
    print(f"系统安全: {sum(r.system_safe for r in results)}/{total}")
    print(f"新能源优先: {sum(r.renewable_priority for r in results)}/{total}")
    print(f"控制器异常: {sum(bool(r.exception) for r in results)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
