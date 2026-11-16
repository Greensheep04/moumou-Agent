"""Rich 终端交互界面（CLI）。

一个终端窗口看群聊 / 私聊 / 审批 / 日志。UI 层不承担任何业务判断：它只负责
把消息渲染出来、把人类的输入包装成 ``HumanResponse`` 传回给 ``CliHuman``。

退出时（``/quit`` 或 Ctrl-C）会取消所有后台协程并清空挂起的审批，保证终端
干净退出（铁律 5）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from rich.console import Console as RichConsole
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from moumou.agents import CliHuman
from moumou.contracts import HUMAN_ID, HumanRequest, HumanResponse, Message, Scope


class Console:
    """Thin Rich wrapper with the four render targets the UI needs."""

    def __init__(self, console: RichConsole | None = None):
        self.rich = console or RichConsole()

    def banner(self, project_id: str = "demo", conversation_id: str = "c1") -> None:
        self.rich.print(
            Panel(
                f"[bold]moumou[/] CLI — project [cyan]{project_id}[/] · "
                f"conversation [cyan]{conversation_id}[/]\n"
                "输入 [green]/history[/] 看历史 · [green]/steer <text>[/] 喊话 · "
                "[green]/quit[/] 退出",
                title="交互与通信层 (P2)",
            )
        )

    def message(self, msg: Message) -> None:
        kind = msg.kind
        if kind == "steer":
            self.rich.print(f"[yellow]→ 喊话[/] [bold]{msg.sender}[/]: {msg.content}")
            return

        # 私聊 vs 群聊的展示差异：私聊带 @目标，广播显示为群消息。
        if msg.recipients:
            to = ", ".join(msg.recipients)
            header = f"[dim]{msg.sender} → {to}[/]"
        else:
            header = f"[dim]{msg.sender} → 群[/]"

        label = {"ask_human": "提问", "approval": "审批", "delegate": "派活",
                 "report": "汇报", "chat": "消息"}.get(kind, kind)
        self.rich.print(f"[bold cyan]{label}[/] {header}")
        self.rich.print(Text(msg.content))
        self.rich.print()

    def notify(self, msg: Message) -> None:
        self.rich.print(f"[magenta]· 系统通知[/] {msg.content}")

    def request(self, req: HumanRequest) -> None:
        kind_text = {"approval": "审批", "question": "提问", "review": "审阅"}.get(
            req.kind, req.kind
        )
        body = [f"[bold]{req.summary}[/]"]
        if req.context:
            body.append(f"[dim]{req.context}[/]")
        if req.action:
            body.append(f"[dim]action: {req.action} ({req.action_class or '-'})[/]")
        if req.payload:
            body.append(f"[dim]payload: {req.payload}[/]")
        self.rich.print(
            Panel(
                "\n".join(body),
                title=f"[bold red]等待人工 · {kind_text}[/]",
                subtitle=f"from {req.agent_id} · {req.id}",
            )
        )


async def _prompt(
    console: Console,
    req: HumanRequest,
    input_fn: Callable[[str], Awaitable[str]],
) -> HumanResponse:
    console.request(req)

    if req.kind == "question":
        text = (await input_fn("回答 > ")).strip()
        return HumanResponse(request_id=req.id, text=text or None)

    # approval / review：默认按 yes/no 决策，也可拒绝并附注。
    decision = await input_fn("同意/拒绝 [y/N] > ")
    decision = decision.strip().lower()
    if decision in ("y", "yes", "同意"):
        return HumanResponse(request_id=req.id, decision="approve", text="")
    return HumanResponse(request_id=req.id, decision="reject", text="rejected from CLI")


async def run_cli(
    bus,
    human: CliHuman,
    *,
    demo: Callable[[object, CliHuman], Awaitable[None]] | None = None,
    console: Console | None = None,
    input_fn: Callable[[str], Awaitable[str]] | None = None,
) -> list[tuple[HumanRequest, HumanResponse]]:
    """Drive the interaction loop until the demo finishes or the user quits."""
    console = console or Console()
    input_fn = input_fn or (lambda prompt: asyncio.to_thread(input, prompt))
    console.banner()

    rendered: asyncio.Queue[Message] = asyncio.Queue()

    def _on_message(msg: Message) -> None:
        # 广播消息所有人都要看到；定向消息只给人类本人。
        if msg.recipients and HUMAN_ID not in msg.recipients:
            return
        rendered.put_nowait(msg)

    bus.subscribe(HUMAN_ID, _on_message)

    demo_task = asyncio.create_task(demo(bus, human)) if demo else None
    answers: list[tuple[HumanRequest, HumanResponse]] = []

    try:
        while True:
            # 1) 渲染到达的广播/定向消息。
            while True:
                try:
                    console.message(rendered.get_nowait())
                except asyncio.QueueEmpty:
                    break

            # 2) 渲染系统通知。
            note = human.poll_notification()
            while note is not None:
                console.notify(note)
                note = human.poll_notification()

            # 3) 处理挂起的审批/提问。
            req = human.poll_request()
            if req is not None:
                resp = await _prompt(console, req, input_fn)
                human.answer(req.id, resp)
                answers.append((req, resp))
                continue

            # 4) 演示结束且没有待处理审批时收尾。
            if demo_task is not None and demo_task.done() and not human.has_pending:
                break

            # 5) 无演示时进入交互命令模式。
            if demo_task is None and rendered.empty():
                cmd = (await input_fn("moumou> ")).strip()
                if cmd in ("/quit", "/q", ""):
                    break
                if cmd == "/history":
                    _print_history(console, bus)
                elif cmd.startswith("/steer "):
                    await _steer(console, bus, cmd[len("/steer "):].strip())
                continue

            await asyncio.sleep(0.02)
    except asyncio.CancelledError:
        raise
    finally:
        if demo_task is not None:
            demo_task.cancel()
            try:
                await demo_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        human.reject_all()
    return answers


def _print_history(console: Console, bus) -> None:
    scope = Scope(kind="group", project_id="demo", conversation_id="c1")
    rows = bus.history(scope, limit=50)
    if not rows:
        console.rich.print("[dim](暂无历史消息)[/]")
        return
    table = Table(title="消息历史", show_lines=False)
    table.add_column("时间", no_wrap=True)
    table.add_column("发送者")
    table.add_column("类型")
    table.add_column("内容")
    for m in rows:
        table.add_row(
            m.ts.strftime("%H:%M:%S"), m.sender, m.kind, m.content[:60]
        )
    console.rich.print(table)


async def _steer(console: Console, bus, text: str) -> None:
    from moumou.contracts import Message

    await bus.publish(
        Message(
            project_id="demo",
            conversation_id="c1",
            sender=HUMAN_ID,
            kind="steer",
            content=text,
        )
    )
    console.rich.print(f"[yellow]已发送喊话：[/]{text}")


async def _scripted_demo(bus, human: CliHuman) -> None:
    """一段不依赖 LLM 的演示：群聊 → 私聊 → 通知 → 审批 → 提问。

    它只演示 P2 的职责范围（消息通路 + 人类交互），不触碰 Prompt / 工具 / LLM。
    """
    from moumou.contracts import HumanRequest, Message

    # 群聊消息（广播）。
    await bus.publish(
        Message(
            project_id="demo",
            conversation_id="c1",
            sender="lead",
            kind="chat",
            content="两个子任务并行开工：一个写代码，一个写文档。",
        )
    )
    await asyncio.sleep(0.1)

    # 私聊给人类。
    await bus.publish(
        Message(
            project_id="demo",
            conversation_id="c1",
            sender="lead",
            recipients=[HUMAN_ID],
            kind="ask_human",
            content="有个动作需要你拍板，请看下面的审批卡片。",
        )
    )
    await human.notify(
        Message(
            project_id="demo",
            conversation_id="c1",
            sender="lead",
            kind="chat",
            content="已生成审批请求，等待人类确认。",
        )
    )

    # 审批（yes/no）。
    await human.ask(
        HumanRequest(
            task_id="t2",
            agent_id="python_dev@t2",
            kind="approval",
            summary="允许执行 shell 命令",
            action="shell_exec",
            action_class="shell",
            payload={"command": "rm -rf build/"},
            context="清理旧构建产物后重新编译。",
        )
    )

    # 自由文本提问。
    await human.ask(
        HumanRequest(
            task_id="t1",
            agent_id="lead@t1",
            kind="question",
            summary="README 里应该用哪个示例命令？",
            context="希望给出一行可直接复制的命令。",
        )
    )


async def main() -> None:
    """``python -m moumou.ui.cli`` 启动交互式演示。"""
    import tempfile
    from pathlib import Path

    from moumou.bus import SqliteBus

    bus = SqliteBus(db_path=Path(tempfile.mkdtemp()) / "cli.db")
    human = CliHuman()
    console = Console()

    answers = await run_cli(bus, human, demo=_scripted_demo, console=console)

    console.rich.print("\n[bold green]演示结束。[/] 收到的回答：")
    for req, resp in answers:
        decision = resp.decision or "(文本)"
        console.rich.print(f"  · {req.kind}: {req.summary!r} → {decision} {resp.text or ''}")


if __name__ == "__main__":
    asyncio.run(main())
