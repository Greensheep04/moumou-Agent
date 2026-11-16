"""持久化消息总线（P2）。

把人类当成 ``agent_id == "human"`` 的普通成员之后，私聊、群聊、人工审批
统一走同一条消息通路。本模块实现 ``Channel``：先保存再投递，可重开历史。
生产接入应绑定 project/conversation 或显式订阅 scope；空成员的 group history
是运维会话视图，不是已认证用户的私聊权限。默认同步回调必须不阻塞。
"""

from .sqlite import SqliteBus

__all__ = ["SqliteBus"]
