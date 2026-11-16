"""持久化消息总线（P2）。

把人类当成 ``agent_id == "human"`` 的普通成员之后，私聊、群聊、人工审批
统一走同一条消息通路。本模块是 ``Channel`` 端口的生产实现：消息先落盘
（SQLite）再投递，进程重启后 ``history()`` 仍能读回历史。
"""

from .sqlite import SqliteBus

__all__ = ["SqliteBus"]
