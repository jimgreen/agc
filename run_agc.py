"""秦岭微电网 AGC 算法后台与 Web HMI 统一启动入口。

默认读取同目录 ``startup.json`` 中的监听地址、模型库、实时库、SVG 与 AGC 数据库路径，
命令行参数可按需覆盖。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web.agc_server import main as serve_main


def main(argv=None) -> int:
    user_args = list(sys.argv[1:] if argv is None else argv)
    startup_file = ROOT / "startup.json"
    defaults: list[str] = []
    if startup_file.is_file():
        cfg = json.loads(startup_file.read_text(encoding="utf-8"))
        mapping = {
            "host": "--host",
            "port": "--port",
            "model_db": "--model-db",
            "scada_db": "--scada-db",
            "svg_path": "--svg",
            "agc_db": "--agc-db",
        }
        for key, flag in mapping.items():
            if key in cfg and cfg[key] is not None:
                defaults.extend([flag, str(cfg[key])])
    return serve_main(defaults + user_args)


if __name__ == "__main__":
    raise SystemExit(main())
