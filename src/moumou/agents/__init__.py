"""人类成员代理（P2）。

人类是总线上 ``agent_id == "human"`` 的普通成员，私聊 / 群聊 / 人工审批共用
同一套机制。``CliHuman`` 是内存队列式 ``HumanIO``，校验原请求与响应并支持取消；
不将取消文字伪装成人答，也不提供进程恢复。正式会话继续使用 P3 的持久文件端口。
"""

from .human import CliHuman, HumanAgent

__all__ = ["CliHuman", "HumanAgent"]
