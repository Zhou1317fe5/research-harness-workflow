#!/usr/bin/env python3
"""兼容旧 skill 脚本入口，行为由公共实现提供。"""
from pathlib import Path
import runpy
import sys

if __name__ == "__main__":
    harness = Path(__file__).resolve().parents[4] / ".agents" / "harness"
    sys.path.insert(0, str(harness))
    runpy.run_path(str(harness / "remote" / "remote_run.py"), run_name="__main__")
