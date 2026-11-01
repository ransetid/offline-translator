"""兼容旧版 translator_v3.py 的 `import records`：实现已迁到 livetranslator/records.py。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from livetranslator.records import *  # noqa: F401,F403,E402
from livetranslator.records import _ROW_KEYS  # noqa: F401,E402
