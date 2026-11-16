"""P2 terminal frontend for saved formal sessions and an explicit offline demo.

The file backend owns approvals, consumption and quality. This UI stages only a
person's explicit response, never starts/resumes an Engine or loads model keys.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from moumou.contracts import HUMAN_ID, HumanRequest, HumanResponse, Message, Scope
from moumou.ui.session import (
    FileSession,
    PendingRequest,
    SessionNotReady,
    validate_response,
)


class Console:
    """Render recorded text without interpreting task/user content as Rich markup."""

    def __init__(self, console=None):
        try:
            from rich.console import Console as RichConsole
            from rich.panel import Panel
            from rich.text import Text
        except ModuleNotFoundError as exc:
            raise RuntimeError("Install the optional P2 interface: python -m pip install -e '.[p2]'") \
                from exc
        self.rich = console if console is not None else RichConsole()
        self._panel, self._text = Panel, Text

    def info(self, text: str) -> None:
        self.rich.print(self._text(text))

    def error(self, text: str) -> None:
        self.rich.print(self._text("错误: " + text, style="red"))

    def banner(self, project_id: str, conversation_id: str, *, mode: str) -> None:
        text = (f"P2 {mode} | project={project_id} | conversation={conversation_id}\n"
                "/pending · /history · /answer ID TEXT · /approve ID [NOTE] · "
                "/reject ID [NOTE] · /quit")
        self.rich.print(self._panel(self._text(text), title="交互与通信"))

    def message(self, msg: Message) -> None:
        target = ", ".join(msg.recipients) if msg.recipients else "群聊"
        self.info(f"[已记录的总线消息] {msg.kind}: {msg.sender} → {target}\n{msg.content}")

    def notify(self, msg: Message) -> None:
        self.info(f"[HumanIO 通知] {msg.sender}: {msg.content}")

    def request(self, req: HumanRequest) -> None:
        body = [f"{req.kind} | 原请求 ID: {req.id}", req.summary,
                f"task={req.task_id} | agent={req.agent_id}"]
        if req.context:
            body.append(req.context)
        if req.action:
            body.append(f"action={req.action} | class={req.action_class or '-'}")
        if req.payload:
            body.append("完整参数: " + json.dumps(req.payload, ensure_ascii=False, allow_nan=False))
        if req.options:
            body.append("选项: " + json.dumps(req.options, ensure_ascii=False))
        command = (f"/answer {req.id} TEXT" if req.kind == "question" else
                   f"/approve {req.id} [NOTE] 或 /reject {req.id} [NOTE]")
        body.append("请明确输入: " + command)
        self.rich.print(self._panel(self._text("\n".join(body)), title="等待实际人类决定"))

    def history_event(self, source: str, line: int, record: dict) -> None:
        self.info(f"[{source}:{line}] " + json.dumps(record, ensure_ascii=False, allow_nan=False))


def _response_command(line: str) -> tuple[str, str, str]:
    command, separator, rest = line.lstrip().partition(" ")
    if command not in {"/answer", "/approve", "/reject"} or not separator:
        raise ValueError("使用 /answer ID TEXT、/approve ID [NOTE] 或 /reject ID [NOTE]")
    request_id, _, text = rest.lstrip().partition(" ")
    if not request_id:
        raise ValueError("必须指定原请求 ID")
    return command, request_id, text


def _response(request: HumanRequest, command: str, text: str) -> HumanResponse:
    if command not in {"/answer", "/approve", "/reject"}:
        raise ValueError("decision command must be explicit")
    if command == "/answer":
        if request.kind != "question":
            raise ValueError("审批或复核必须使用 /approve 或 /reject")
        value = HumanResponse(request_id=request.id, text=text)
    else:
        if request.kind == "question":
            raise ValueError("业务澄清只能用 /answer，不能变成操作授权")
        value = HumanResponse(request_id=request.id,
                              decision="approve" if command == "/approve" else "reject",
                              text=text or None)
    return validate_response(request, value)


async def _cancel_input(task):
    if task is None:
        return
    if not task.done():
        task.cancel()
    try:
        await task
    except (asyncio.CancelledError, EOFError):
        pass


async def run_session(
    session: FileSession,
    *,
    console: Console | None = None,
    input_fn: Callable[[str], Awaitable[str]],
    poll_interval_s: float = 0.2,
) -> None:
    """Attach until /quit or EOF. Detaching leaves the backend and pending state alone."""
    if poll_interval_s <= 0:
        raise ValueError("poll interval must be positive")
    console = console or Console()
    console.info(f"附加文件协议: {session.root}")
    console.info("界面不启动或续跑后台。pending 不证明进程存活；暂存答复不证明后台已消费。")
    current = None
    shown = {}
    displayed: dict[str, PendingRequest] = {}
    waiting_notice = None
    input_task = None
    try:
        while True:
            try:
                context = session.current()
                if current != context:
                    current = context
                    shown.clear()
                    displayed.clear()
                    console.banner(context.scenario_id, context.conversation_id,
                                   mode="正式文件会话 / " + context.phase)
                    console.info("保存的整场状态: " + str(session.manifest().get("status", "unknown")))
                for item in session.pending():
                    if item.context != context:
                        continue  # Let the next poll update the phase banner first.
                    displayed[item.request_id] = item
                    if shown.get(item.request_id) != item.archive_sha256:
                        console.request(item.request)
                        shown[item.request_id] = item.archive_sha256
                waiting_notice = None
            except SessionNotReady as exc:
                if str(exc) != waiting_notice:
                    console.info(str(exc))
                    waiting_notice = str(exc)
            if input_task is None:
                input_task = asyncio.create_task(input_fn("moumou> "))
            done, _ = await asyncio.wait({input_task}, timeout=poll_interval_s)
            if not done:
                continue
            try:
                line = input_task.result().rstrip("\r\n")
            except EOFError:
                console.info("输入已关闭；仅分离界面，后台待答记录保持原样。")
                return
            input_task = None
            command = line.strip()
            if command in {"/quit", "/q"}:
                console.info("已分离界面；没有回答待答问题或取消正式后台。")
                return
            if not command:
                continue
            try:
                if command == "/history":
                    records = session.history()
                    if not records:
                        console.info("当前场景还没有保存的历史事件。")
                    for event in records:
                        console.history_event(event.source, event.line_number, event.record)
                elif command == "/pending":
                    pending = session.pending()
                    if not pending:
                        console.info("当前阶段没有尚未暂存/消费的待答请求；这不证明进程已完成。")
                    for item in pending:
                        displayed[item.request_id] = item
                        console.request(item.request)
                elif command.startswith("/steer"):
                    console.info("现有 P3 文件协议不支持 steer 传输；没有发送或创建替代队列。")
                else:
                    action, request_id, text = _response_command(line)
                    item = displayed.get(request_id)
                    if item is None:
                        raise ValueError("未找到当前已展示的原请求；请先 /pending 核对 ID 和范围")
                    result = session.stage_response(item, _response(item.request, action, text))
                    console.info(f"原请求 {result.request_id}: {result.state}。"
                                 "只有后台消费后才会产生正式响应/审批/复核记录。")
            except (ValueError, SessionNotReady) as exc:
                console.error(str(exc))
    finally:
        await _cancel_input(input_task)


async def run_cli(
    bus,
    human,
    *,
    project_id: str,
    conversation_id: str,
    demo: Callable[[object, object], Awaitable[None]] | None = None,
    console: Console | None = None,
    input_fn: Callable[[str], Awaitable[str]],
    poll_interval_s: float = 0.05,
) -> list[tuple[HumanRequest, HumanResponse]]:
    """Render one explicitly selected bus scope; caller retains ownership of the bus."""
    if not project_id or not conversation_id or poll_interval_s <= 0:
        raise ValueError("explicit project/conversation and positive polling are required")
    if (getattr(human, "project_id", None), getattr(human, "conversation_id", None)) != (
        project_id, conversation_id,
    ):
        raise ValueError("use a CliHuman bound to this exact project/conversation")
    console = console or Console()
    console.banner(project_id, conversation_id, mode="脚本演示" if demo else "指定总线")
    scope = Scope(project_id=project_id, conversation_id=conversation_id)
    rendered: asyncio.Queue[Message] = asyncio.Queue()
    requests = {}
    answers = []
    input_task = None
    closed = False

    def on_message(msg):
        if (closed or msg.project_id != project_id or msg.conversation_id != conversation_id
                or (msg.recipients and HUMAN_ID not in msg.recipients and msg.sender != HUMAN_ID)):
            return
        rendered.put_nowait(msg)

    bus.subscribe(HUMAN_ID, on_message, scope=scope)
    demo_task = asyncio.create_task(demo(bus, human)) if demo is not None else None
    try:
        while True:
            while not rendered.empty():
                console.message(rendered.get_nowait())
            while (note := human.poll_notification()) is not None:
                if (note.project_id, note.conversation_id) != (project_id, conversation_id):
                    raise ValueError("notification belongs to another UI scope")
                console.notify(note)
            while (request := human.poll_request()) is not None:
                if (request.project_id, request.conversation_id) != (project_id, conversation_id):
                    raise ValueError("human request belongs to another UI scope")
                requests[request.id] = request
                console.request(request)
            if demo_task is not None and demo_task.done():
                await demo_task  # Surface real demo exceptions, not a success banner.
                if not human.has_pending:
                    return answers
            if input_task is None:
                input_task = asyncio.create_task(input_fn("moumou> "))
            done, _ = await asyncio.wait({input_task}, timeout=poll_interval_s)
            if not done:
                continue
            try:
                line = input_task.result().rstrip("\r\n")
            except EOFError:
                return answers
            input_task = None
            command = line.strip()
            if command in {"/quit", "/q"}:
                return answers
            if not command:
                continue
            try:
                if command == "/history":
                    rows = bus.history(scope, limit=50)
                    for index, msg in enumerate(rows, 1):
                        if not msg.recipients or HUMAN_ID in msg.recipients or msg.sender == HUMAN_ID:
                            console.history_event("demo message bus" if demo else "selected message bus",
                                                  index, msg.model_dump(mode="json"))
                elif command == "/pending":
                    for request in requests.values():
                        console.request(request)
                elif command.startswith("/steer"):
                    console.info("此界面未绑定运行中 steer 消费者，没有发送控制消息。")
                else:
                    action, request_id, text = _response_command(line)
                    request = requests.get(request_id)
                    if request is None:
                        raise ValueError("未知或尚未展示的请求 ID")
                    response = _response(request, action, text)
                    if not human.answer(request_id, response):
                        raise ValueError("该请求已撤回或处理，未提交另一份答复")
                    answers.append((request, response))
                    requests.pop(request_id)
            except ValueError as exc:
                console.error(str(exc))
    finally:
        closed = True
        try:
            bus.unsubscribe(HUMAN_ID, on_message)
        finally:
            try:
                await _cancel_input(input_task)
            finally:
                try:
                    if demo_task is not None:
                        if not demo_task.done():
                            demo_task.cancel()
                            await asyncio.gather(demo_task, return_exceptions=True)
                        else:
                            await demo_task
                finally:
                    # Cancellation is not a fabricated rejection or quality decision.
                    human.cancel_all()


async def _scripted_demo(bus, human) -> None:
    """Explicit simulation: group/DM/notification/approval/question, with no tools or model."""
    project, conversation = "demo", "c1"
    await bus.publish(Message(project_id=project, conversation_id=conversation, sender="lead",
                              content="这是 P2 脚本演示，不是老师任务执行。"))
    await bus.publish(Message(project_id=project, conversation_id=conversation, sender="lead",
                              recipients=[HUMAN_ID], kind="ask_human",
                              content="请根据原请求 ID 使用 /approve 或 /reject。"))
    await human.notify(Message(project_id=project, conversation_id=conversation, sender="lead",
                               content="演示请求不会执行真实 shell 或模型调用。"))
    await human.ask(HumanRequest(project_id=project, conversation_id=conversation,
                                role="lead", task_id="demo-approval", agent_id="lead",
                                kind="approval", summary="是否继续脚本演示？",
                                action="demo_continue", options=["approve", "reject"],
                                payload={"simulation_only": True}))
    await human.ask(HumanRequest(project_id=project, conversation_id=conversation,
                                role="lead", task_id="demo-question", agent_id="lead",
                                kind="question", summary="演示页面采用什么标题？"))


async def _start(args, console):
    from moumou.ui.input import TerminalInput

    if args.run is not None:
        session = FileSession(args.run)
        async with TerminalInput() as input_fn:
            await run_session(session, console=console, input_fn=input_fn)
        return
    from uuid import uuid4

    from moumou.agents import CliHuman
    from moumou.bus import SqliteBus

    directory = Path("runs/p2-demo") / uuid4().hex[:12]
    bus = SqliteBus(db_path=directory / "messages.db", project_id="demo", conversation_id="c1")
    human = CliHuman(project_id="demo", conversation_id="c1")
    console.info(f"仅运行显式离线演示；消息数据库: {directory / 'messages.db'}")
    try:
        async with TerminalInput() as input_fn:
            await run_cli(bus, human, project_id="demo", conversation_id="c1",
                          demo=_scripted_demo, console=console, input_fn=input_fn)
        console.info("脚本演示结束；不构成正式题库或真实 Agent 执行结果。")
    finally:
        try:
            await human.aclose()
        finally:
            await bus.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P2 frontend for an existing P3 file session")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--run", type=Path, help="existing formal RUN launched with --human files")
    modes.add_argument("--demo", action="store_true", help="explicit offline scripted P2 simulation")
    args = parser.parse_args(argv)
    if args.run is None and not args.demo:
        parser.print_help()
        return 0
    try:
        asyncio.run(_start(args, Console()))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("界面已停止；没有为正式后台补造答复或质量决定。", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"P2 interface: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
