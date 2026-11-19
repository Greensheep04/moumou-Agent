"""Root delivery closure is explicit, bounded and separate from factual quality."""

from __future__ import annotations

from pathlib import Path

import pytest

from moumou.adapters import FakeLLM
from moumou.adapters.llm import LLMResponse, ToolCall
from moumou.contracts import Budget, TaskNode, TaskResult
from moumou.eval import delivery_quality
from moumou.eval.delivery_quality import (
    CONTENT_QUALITY_RULES,
    DeliveryQualityError,
    validate_delivery,
)
from moumou.orchestrator import Engine, Settings


def root_node(**changes):
    return TaskNode(id="t1", project_id="p", conversation_id="c", role="writer",
                    goal="Deliver the requested evidence and presentation.", **changes)


def write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("existing", [False, True])
def test_three_companion_reports_must_be_explicit_and_are_never_auto_exported(tmp_path, existing):
    names = ["research-notes.md", "risk-notes.txt", "client-brief.md"]
    write(tmp_path, "final.html", "<html>\n<p>Supporting files:</p>\n<ul>\n" + "\n".join(
        f"<li><code>{name}</code></li>" for name in names
    ) + "\n</ul></html>")
    if existing:
        for name in names:
            write(tmp_path, name, "Original supporting evidence.")
    result = TaskResult(files=["final.html"])
    with pytest.raises(DeliveryQualityError) as error:
        validate_delivery(root_node(), tmp_path, result)
    assert all(name in str(error.value) for name in names)
    assert ("undeclared_reference" if existing else "missing_reference") in str(error.value)
    assert "final.html:" in str(error.value)
    assert result.files == ["final.html"]
    for name in names:
        write(tmp_path, name, "Original supporting evidence.")
    result.files.extend(names)
    validate_delivery(root_node(), tmp_path, result)


def test_markdown_urls_anchors_reference_links_and_code_examples_are_not_missing_files(tmp_path):
    write(tmp_path, "README.md", """# Delivery
[local](notes/evidence.txt#detail)
[citation][evidence]
[evidence]: notes/evidence.txt
[unused]: not-a-deliverable.md
[remote](https://example.org/remote-report.md)
[section](#appendix.md)
See https://example.org/other.txt for context.
Read `cat sample.md` as an example command.
```md
[example](nonexistent.md)
```
    [indented example](not-real.md)
<!-- [private example](.evaluation/context.md) -->
""")
    write(tmp_path, "notes/evidence.txt", "Observed evidence.")
    validate_delivery(root_node(), tmp_path, TaskResult(files=["README.md", "notes/evidence.txt"]))


def test_existing_software_readme_commands_and_api_routes_remain_valid(tmp_path):
    write(tmp_path, "README.md", """# Application
[source](application.py)
Run `python application.py --db example.db`.
```text
python application.py write-report missing-example.md
```
""")
    write(tmp_path, "dashboard.html", """<a href="/api/status">Status</a>
<a href="#history">History</a><script>fetch('/api/summary.json')</script>
<pre><code>[example](missing.md)</code></pre>
<img src="data:image/svg+xml;base64,PHN2Zy8+">
""")
    write(tmp_path, "application.py", "print('application')")
    validate_delivery(root_node(), tmp_path,
                      TaskResult(files=["README.md", "dashboard.html", "application.py"]))


@pytest.mark.parametrize("existing", [False, True])
def test_indented_html_asset_cannot_be_missing_or_undeclared(tmp_path, existing):
    write(tmp_path, "index.html", "<html>\n    <img src='assets/chart.svg#figure'>\n</html>")
    if existing:
        write(tmp_path, "assets/chart.svg", "<svg/>")
    with pytest.raises(DeliveryQualityError, match="index.html:2"):
        validate_delivery(root_node(), tmp_path, TaskResult(files=["index.html"]))


def test_html_and_declared_css_asset_closure_uses_each_source_directory(tmp_path):
    files = {
        "index.html": """<html>
    <link rel="stylesheet" href="assets/site.css">
    <script src="assets/app.js"></script>
    <img srcset="assets/small.svg 1x, assets/large.svg 2x">
    <img src="https://example.org/remote.png">
    <style>body { background: url('assets/background.svg'); }</style>
</html>""",
        "assets/site.css": "@import 'font.css'; body { background: url(icon.svg); }",
        "assets/font.css": "/* typography */",
        "assets/app.js": "// actual application code",
        "assets/small.svg": "<svg/>", "assets/large.svg": "<svg/>",
        "assets/background.svg": "<svg/>", "assets/icon.svg": "<svg/>",
    }
    for relative, text in files.items():
        write(tmp_path, relative, text)
    result = TaskResult(files=list(files))
    validate_delivery(root_node(), tmp_path, result)
    result.files.remove("assets/icon.svg")
    with pytest.raises(DeliveryQualityError, match="assets/site.css:1.*assets/icon.svg"):
        validate_delivery(root_node(), tmp_path, result)


def test_external_html_base_keeps_relative_remote_assets_outside_file_closure(tmp_path):
    write(tmp_path, "index.html",
          '<base href="https://example.org/"><script src="app.js"></script>')
    validate_delivery(root_node(), tmp_path, TaskResult(files=["index.html"]))


def test_source_notes_distinguish_local_companions_from_unprefixed_public_citations(tmp_path):
    write(tmp_path, "deck.html", """<p>
    <span>Source: investigation_notes.md; risk_memo.txt.</span>
    <span>Source: finance.example.com/news/press-release.html, example.org/news.</span>
    <span>Synthesis of investigation_notes.md, risk_memo.txt, layout_notes.md.</span>
</p>""")
    with pytest.raises(DeliveryQualityError) as error:
        validate_delivery(root_node(), tmp_path, TaskResult(files=["deck.html"]))
    feedback = str(error.value)
    assert all(name in feedback for name in (
        "investigation_notes.md", "risk_memo.txt", "layout_notes.md",
    ))
    assert "press-release.html" not in feedback and "example.com" not in feedback


def test_remote_links_do_not_use_the_local_reference_inspection_limit(tmp_path, monkeypatch):
    write(tmp_path, "delivery.md", "\n".join(
        f"[remote](https://example.org/source-{index}.md)" for index in range(8)
    ) + "\n[local](evidence.txt)")
    write(tmp_path, "evidence.txt", "Observed evidence.")
    monkeypatch.setattr(delivery_quality, "_MAX_REFERENCES", 1)
    validate_delivery(root_node(), tmp_path, TaskResult(files=["delivery.md", "evidence.txt"]))


def test_repeated_references_need_one_action_and_explicit_space_paths_are_supported(tmp_path):
    write(tmp_path, "delivery.md", "[one](<notes/Source Notes.md>)\n[two](<notes/Source Notes.md>)")
    write(tmp_path, "notes/Source Notes.md", "Original evidence.")
    with pytest.raises(DeliveryQualityError) as error:
        validate_delivery(root_node(), tmp_path, TaskResult(files=["delivery.md"]))
    assert str(error.value).count("undeclared_reference") == 1
    validate_delivery(root_node(), tmp_path,
                      TaskResult(files=["delivery.md", "notes/Source Notes.md"]))


@pytest.mark.parametrize("target,classification", [
    (".evaluation/context.json", "private_reference"),
    (".git/config", "private_reference"),
    (".env", "private_reference"),
    ("%2eenv.local", "private_reference"),
    ("../outside.txt", "unsafe_reference"),
    ("file:///C:/outside.txt", "unsafe_reference"),
    ("C:/outside.txt", "unsafe_reference"),
])
def test_private_and_escaping_references_are_rejected_without_reading_target(
        tmp_path, monkeypatch, target, classification):
    source = write(tmp_path, "delivery.md", f"[supporting document]({target})")
    original = Path.open
    opened = []

    def only_declared_document(path, *args, **kwargs):
        opened.append(path)
        assert path == source, "undeclared/private targets must never be read"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", only_declared_document)
    with pytest.raises(DeliveryQualityError, match=classification):
        validate_delivery(root_node(), tmp_path, TaskResult(files=["delivery.md"]))
    assert opened == [source]


def test_symlink_to_outside_workspace_is_not_an_export_permission(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = write(tmp_path, "outside.txt", "private content")
    try:
        (workspace / "linked.txt").symlink_to(outside)
    except OSError:
        pytest.skip("host does not permit symlinks")
    write(workspace, "delivery.md", "[supporting document](linked.txt)")
    with pytest.raises(DeliveryQualityError, match="unsafe_reference"):
        validate_delivery(root_node(), workspace, TaskResult(files=["delivery.md"]))


def test_declared_symlink_to_secret_is_rejected_before_reading(tmp_path, monkeypatch):
    secret = write(tmp_path, ".env.local", "not for delivery")
    try:
        (tmp_path / "public.md").symlink_to(secret)
    except OSError:
        pytest.skip("host does not permit symlinks")

    def forbid_read(*args, **kwargs):
        pytest.fail("declared secret contents must never be read")

    monkeypatch.setattr(Path, "open", forbid_read)
    with pytest.raises(DeliveryQualityError, match="private_declaration"):
        validate_delivery(root_node(), tmp_path, TaskResult(files=["public.md"]))


def test_reference_metadata_errors_return_correctable_feedback(tmp_path, monkeypatch):
    write(tmp_path, "delivery.md", "See support.txt")
    target = write(tmp_path, "support.txt", "Support")
    original = Path.is_file

    def unavailable(path):
        if path == target:
            raise PermissionError("metadata temporarily unavailable")
        return original(path)

    monkeypatch.setattr(Path, "is_file", unavailable)
    result = TaskResult(files=["delivery.md"])
    with pytest.raises(DeliveryQualityError, match="unreadable_reference.*delivery.md:1"):
        validate_delivery(root_node(), tmp_path, result)
    assert result.files == ["delivery.md"]


def test_bounded_reader_does_not_silently_accept_an_uninspected_document(tmp_path, monkeypatch):
    write(tmp_path, "large.md", "x" * 80)
    monkeypatch.setattr(delivery_quality, "_MAX_DOCUMENT_BYTES", 32)
    with pytest.raises(DeliveryQualityError, match="scan_limit"):
        validate_delivery(root_node(), tmp_path, TaskResult(files=["large.md"]))


def test_children_are_not_subject_to_root_closure_rules(tmp_path):
    write(tmp_path, "part.md", "See incomplete-draft.md")
    validate_delivery(root_node(parent_id="parent", depth=1), tmp_path,
                      TaskResult(files=["part.md"]))


def test_quality_protocol_is_generic_and_small_enough_for_mandatory_context():
    assert len(CONTENT_QUALITY_RULES) < 3500
    assert "percentage-point" in CONTENT_QUALITY_RULES and "unverified" in CONTENT_QUALITY_RULES
    assert "word/page quotas" in CONTENT_QUALITY_RULES


def answer(files):
    return LLMResponse(tool_calls=[ToolCall(id="deliver", name="submit_result",
                                          args={"summary": "Requested delivery", "files": files})])


@pytest.mark.parametrize("enabled", [False, True])
async def test_optional_worker_hook_returns_feedback_before_result_or_artifact_publication(
        tmp_path, monkeypatch, enabled):
    calls = 0

    def script(messages, tools):
        nonlocal calls
        calls += 1
        if calls == 1:
            return answer(["delivery.md"])
        assert enabled and calls == 2
        assert "undeclared_reference" in messages[-1]["content"]
        assert "invalid_result_files" in messages[-1]["content"]
        assert run.repo.get(node.id).result is None and run.repo.get(node.id).artifacts == []
        assert not [event for event in run.trace.query(node.id) if event.kind == "acceptance"]
        return answer(["delivery.md", "support.txt"])

    with Engine(FakeLLM(script), settings=Settings(
        root=tmp_path / "run", project_id="p", conversation_id="c",
        memory_enabled=False, role_review_enabled=False,
    )) as run:
        if enabled:
            monkeypatch.setattr(run.context, "validate_delivery", validate_delivery, raising=False)
        node = run.submit("Publish explicit supporting files.", budget=Budget(max_retries=0))
        workspace = run.workspace.allocate("p", "c", node.role + "@" + node.id)
        write(workspace, "delivery.md", "See support.txt")
        write(workspace, "support.txt", "Original support.")
        await run.run()
        saved = run.repo.get(node.id)
        assert saved.status == "DONE", saved.error
        assert calls == (2 if enabled else 1)
        assert saved.result.files == (
            ["delivery.md", "support.txt"] if enabled else ["delivery.md"]
        )


async def test_hook_rechecks_final_bytes_after_acceptance_before_publication(tmp_path, monkeypatch):
    calls, checks = 0, 0

    def script(messages, tools):
        nonlocal calls
        calls += 1
        if calls == 2:
            assert "undeclared_reference" in messages[-1]["content"]
            assert run.repo.get(node.id).result is None and run.repo.get(node.id).artifacts == []
        return answer(["delivery.md"] if calls == 1 else ["delivery.md", "support.txt"])

    async def acceptance(node, workspace):
        nonlocal checks
        checks += 1
        write(workspace, "delivery.md", "See support.txt")
        return True, []

    with Engine(FakeLLM(script), settings=Settings(
        root=tmp_path / "run", project_id="p", conversation_id="c",
        memory_enabled=False, role_review_enabled=False,
    )) as run:
        monkeypatch.setattr(run.context, "validate_delivery", validate_delivery, raising=False)
        monkeypatch.setattr(run.acceptance, "check", acceptance)
        node = run.submit("Validate the final delivery bytes.", budget=Budget(max_retries=0))
        workspace = run.workspace.allocate("p", "c", node.role + "@" + node.id)
        write(workspace, "delivery.md", "Initial document without a reference.")
        write(workspace, "support.txt", "Existing support.")
        await run.run()
        assert run.repo.get(node.id).status == "DONE"
        assert calls == checks == 2
