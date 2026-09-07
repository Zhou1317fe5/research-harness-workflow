"""统一定位项目根目录与配置目录，不依赖启动时的工作目录。"""
from pathlib import Path

HARNESS_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = HARNESS_ROOT.parents[1]
CONFIG_DIR = HARNESS_ROOT / "config"
