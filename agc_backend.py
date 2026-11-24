"""秦岭 AGC 独立算法后台进程入口。

可独立于 Web 服务运行，按设定周期采集 ``scdfe_d.sqlite3``、执行交直流与氢能协同
控制决策算法、在闭环模式下下发四遥指令，并将控制参数、计算策略、趋势曲线与日志
持久化写入 ``D:\\codex\\workspace\\realtime\\qinling\\sqldb\\agc\\agc.sqlite3``。

示例：
    python agc_backend.py --once
    python agc_backend.py --loop --mode open --interval 2
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path

from agc_datasources import (
    DEFAULT_AGC_DB,
    DEFAULT_MODEL_DB,
    DEFAULT_SCADA_DB,
    DEFAULT_SVG_PATH,
)
from agc_service import AGCRealtimeService


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="秦岭微电网 AGC 独立算法后台")
    parser.add_argument("--model-db", default=str(DEFAULT_MODEL_DB), help="输入模型数据库路径 (devices.sqlite3)")
    parser.add_argument("--scada-db", default=str(DEFAULT_SCADA_DB), help="实时数据数据库路径 (scdfe_d.sqlite3)")
    parser.add_argument("--svg", default=str(DEFAULT_SVG_PATH), help="SVG 接线图或关联 .e 模型路径")
    parser.add_argument("--agc-db", default=str(DEFAULT_AGC_DB), help="AGC 参数/策略/曲线/日志持久化库路径")
    parser.add_argument("--mode", choices=["open", "closed"], default=None, help="覆盖开环/闭环模式")
    parser.add_argument("--interval", type=float, default=None, help="覆盖自动控制周期（秒）")
    parser.add_argument("--once", action="store_true", help="仅执行一次策略计算并输出摘要后退出")
    parser.add_argument("--loop", action="store_true", help="持续周期运行 AGC 算法后台")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    service = AGCRealtimeService(
        model_db=Path(args.model_db),
        scada_db=Path(args.scada_db),
        svg_path=Path(args.svg),
        agc_db=Path(args.agc_db),
        start_background=False,
    )
    try:
        if args.mode is not None:
            service.set_loop_mode(args.mode)
        if args.interval is not None and args.interval >= 1.0:
            service.update_control_parameters({"interval_seconds": float(args.interval)}, preset=service.preset)

        if args.once or not args.loop:
            res = service.run_control_cycle(trigger="manual")
            latest = res.get("latest") or {}
            print(
                json.dumps(
                    {
                        "cycle": res.get("cycle"),
                        "loop_mode": res.get("loop_mode"),
                        "agc_db": res.get("agc_db"),
                        "data_quality": latest.get("data_quality"),
                        "command_count": latest.get("command_count"),
                        "changed_count": latest.get("changed_count"),
                        "dispatched_count": latest.get("dispatched_count"),
                        "summary": latest.get("summary_text"),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        stop_flag = False

        def _handle_signal(_sig, _frame):
            nonlocal stop_flag
            stop_flag = True

        signal.signal(signal.SIGINT, _handle_signal)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _handle_signal)

        service.set_auto_enabled(True)
        print(
            f"[AGC Backend] 启动成功 | 模型={args.model_db} | 实时库={args.scada_db} | AGC库={args.agc_db} | 模式={service.loop_mode}",
            flush=True,
        )
        while not stop_flag:
            res = service.run_control_cycle(trigger="auto")
            latest = res.get("latest") or {}
            print(
                f"[AGC Backend] 周期 #{res.get('cycle')} | {latest.get('summary_text', '')}",
                flush=True,
            )
            wait_s = max(1.0, float(service.settings.interval_seconds))
            deadline = time.monotonic() + wait_s
            while not stop_flag and time.monotonic() < deadline:
                time.sleep(0.25)
        return 0
    finally:
        service.close()


if __name__ == "__main__":
    raise SystemExit(main())
