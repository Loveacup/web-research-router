#!/usr/bin/env python3
"""wrr-cli: Web Research Router 命令行入口（薄封装）。

直接调用 wrr._cli.main()，保持独立 CLI 兼容。
pip 安装后使用 ``wrr`` 命令等价。
"""

import sys
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from wrr._cli import main

if __name__ == "__main__":
    sys.exit(main())
