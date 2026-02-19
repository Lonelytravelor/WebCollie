"""兼容层：请改用 src/collie_package/rd_selftest 实现。"""

from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / 'src'
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from collie_package.rd_selftest import *  # noqa: F401,F403
