"""兼容桥接：复用 rd_selftest.collie_automation.log_tools。"""

from ..collie_automation.log_tools.log_analyzer import *  # noqa: F401,F403
from ..collie_automation.log_tools import log_analyzer  # noqa: F401

