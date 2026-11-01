# -*- coding: utf-8 -*-
"""桌面控制台入口（双击运行，无控制台窗口）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from wxagent.gui import main

if __name__ == "__main__":
    sys.exit(main())