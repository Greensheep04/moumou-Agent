"""Written UI checks; scripted fixtures are not formal human-evaluation evidence."""

from __future__ import annotations

import asyncio
import io
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("rich", reason="install the optional p2 extra for interface checks")

from test_p2_session import make_session  # noqa: E402 - optional UI dependency gate above

from moumou.agents import CliHuman  # noqa: E402
from moumou.contracts import HumanRequest  # noqa: E402
from moumou.ui.cli import main, run_cli, run_session  # noqa: E402
from moumou.ui.input import TerminalInput  # noqa: E402


class Display:
    def __init__(self):
        self.lines = []
        self.requests = []
        self.histories = []

    def info(self, text):
        self.lines.append(text)

    def error(self, text):
        self.lines.append(text)

    def banner(self, project_id, conversation_id, *, mode):
        self.lines.append((project_id, conversation_id, mode))

    def request(self, request):
        self.requests.append(request)

    def history_event(self, source, line, record):
        self.histories.append((source, line, record))

    def message(self, message):
        self.lines.append(message.content)

    def notify(self, message):
        self.lines.append(message.content)


def test_no_mode_prints_help_without_starting_any_backend(capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("help unexpectedly started an event loop")

    monkeypatch.setattr("moumou.ui.cli.asyncio.run", forbidden)
    assert main([]) == 0
    assert "--run" in capsys.readouterr().out


async def test_quit_and_unsupported_steer_never_answer_or_cancel_the_real_backend(tmp_path):
    root, session, _ = make_session(tmp_path)
    display = Display()
    commands = iter(["/steer change the task", "/quit"])

    async def input_fn(prompt):
        return next(commands)

    await run_session(session, console=display, input_fn=input_fn, poll_interval_s=0.001)
    assert list((root / "scenario_1/human/responses").iterdir()) == []
    assert (root / "scenario_1/human/pending/original-question.json").exists()
    assert not list(root.rglob("*steer*"))
    assert any("不支持 steer" in line for line in display.lines if isinstance(line, str))


async def test_explicit_request_id_stages_answer_and_reports_transport_state(tmp_path):
    root, session, _ = make_session(tmp_path)
    commands = iter(["/answer unknown old answer", "/answer original-question keep history", "/quit"])
    display = Display()

    async def input_fn(prompt):
        return next(commands)

    await run_session(session, console=display, input_fn=input_fn, poll_interval_s=0.001)
    staged = root / "scenario_1/human/responses/original-question.json"
    assert staged.exists() and "keep history" in staged.read_text(encoding="utf-8")
    assert not (root / "scenario_1/human/consumed/original-question.json").exists()
    assert any("staged" in line for line in display.lines if isinstance(line, str))


async def test_cancelling_ui_cancels_its_input_wait_and_preserves_backend_request(tmp_path):
    root, session, _ = make_session(tmp_path)
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def input_fn(prompt):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    running = asyncio.create_task(run_session(session, console=Display(), input_fn=input_fn,
                                               poll_interval_s=0.001))
    await entered.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert cancelled.is_set()
    assert (root / "scenario_1/human/pending/original-question.json").exists()
    assert list((root / "scenario_1/human/responses").iterdir()) == []


async def test_demo_accepts_commands_while_waiting_and_cancels_without_fake_rejection(tmp_path):
    from moumou.bus import SqliteBus

    bus = SqliteBus(db_path=tmp_path / "bus.db", project_id="p", conversation_id="c")
    human = CliHuman(project_id="p", conversation_id="c")
    display = Display()
    requested = asyncio.Event()
    wait_cancelled = asyncio.Event()

    async def demo(bus, human):
        requested.set()
        try:
            await human.ask(HumanRequest(id="demo-permission", project_id="p", conversation_id="c",
                                        role="lead", task_id="t1", agent_id="lead", kind="approval",
                                        summary="Only a scripted fixture"))
        finally:
            wait_cancelled.set()

    async def input_fn(prompt):
        await requested.wait()
        return "/quit"

    try:
        answers = await run_cli(bus, human, project_id="p", conversation_id="c", demo=demo,
                                console=display, input_fn=input_fn, poll_interval_s=0.001)
        assert answers == [] and wait_cancelled.is_set() and not human.has_pending
    finally:
        await human.aclose()
        await bus.aclose()


async def test_windows_poll_input_cancels_without_waiting_on_an_executor_thread(monkeypatch):
    fake_console = SimpleNamespace(kbhit=lambda: False)
    monkeypatch.setitem(sys.modules, "msvcrt", fake_console)
    reader = TerminalInput(stdout=io.StringIO())
    waiting = asyncio.create_task(reader._windows_line())
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting


@pytest.mark.parametrize("failure", ["input", "demo"])
async def test_demo_cleanup_runs_even_when_input_or_demo_fails_with_pending_question(tmp_path, failure):
    from moumou.bus import SqliteBus

    bus = SqliteBus(db_path=tmp_path / "bus.db", project_id="p", conversation_id="c")
    human = CliHuman(project_id="p", conversation_id="c")
    entered = asyncio.Event()
    asks = []

    async def demo(bus, human):
        waiting = asyncio.create_task(human.ask(HumanRequest(
            id="pending-during-error", project_id="p", conversation_id="c", role="lead",
            task_id="t1", agent_id="lead", kind="question", summary="No fake answer",
        )))
        asks.append(waiting)
        await asyncio.sleep(0)
        entered.set()
        if failure == "demo":
            raise RuntimeError("synthetic demo failure")
        await waiting

    async def input_fn(prompt):
        await entered.wait()
        if failure == "input":
            raise OSError("synthetic input failure")
        await asyncio.Event().wait()

    expected = OSError if failure == "input" else RuntimeError
    try:
        with pytest.raises(expected, match="synthetic"):
            await run_cli(bus, human, project_id="p", conversation_id="c", demo=demo,
                          console=Display(), input_fn=input_fn, poll_interval_s=0.001)
        assert not human.has_pending
        await asyncio.gather(*asks, return_exceptions=True)
        assert all(task.cancelled() for task in asks)
    finally:
        await human.aclose()
        await asyncio.gather(*asks, return_exceptions=True)
        await bus.aclose()
