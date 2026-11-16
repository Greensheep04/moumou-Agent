"""人类成员代理（P2）。

人类是总线上 ``agent_id == "human"`` 的普通成员，私聊 / 群聊 / 人工审批共用
同一套机制。这里的 ``CliHuman`` 是 ``HumanIO`` 端口的交互式实现：``ask`` 借助
``asyncio.Future`` 异步挂起等待真人输入，并且在任务被 ``cancel()`` 时优雅
撤回、不留下脏状态。
"""

from .human import CliHuman, HumanAgent

__all__ = ["CliHuman", "HumanAgent"]
