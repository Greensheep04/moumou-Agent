"""交互界面（P2）。

CLI 负责把总线上的人和 Agent 对话、系统通知、以及 ``ask_human`` 审批卡片
渲染到终端；``replay`` 负责把一次任务的 trace 从头到尾重放。

这里用惰性导入：直接 ``python -m moumou.ui.cli`` / ``-m moumou.ui.replay``
时不会因为包初始化提前导入子模块而产生 ``runpy`` 警告。
"""

from __future__ import annotations

__all__ = ["run_cli", "replay"]


def __getattr__(name: str):
    if name == "run_cli":
        from .cli import run_cli

        return run_cli
    if name == "replay":
        from .replay import replay

        return replay
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
