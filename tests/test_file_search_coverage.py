"""Do not mistake a bounded negative file search for exhaustive evidence."""

import json

from moumou.contracts import ToolContext
from moumou.tools.filesystem import ReadFileTool, SearchFilesTool


def context(root):
    return ToolContext(agent_id="reviewer", task_id="review", workspace=root)


async def test_large_archive_is_reported_and_remains_readable_by_exact_path(tmp_path):
    archive = tmp_path / "prior-dialogue.txt"
    archive.write_bytes(b"needle\n" + b"x" * (1024 * 1024))
    result = await SearchFilesTool().run({"query": "needle"}, context(tmp_path))
    assert result.ok and json.loads(result.stdout) == []
    coverage = json.loads(result.stderr)
    assert coverage["files_started"] == 0
    assert coverage["observed_skipped_large_files"] == 1
    assert coverage["skipped_large_paths"] == ["prior-dialogue.txt"]
    assert coverage["walk_exhausted"] and not result.truncated
    page = await ReadFileTool().run(
        {"path": "prior-dialogue.txt", "offset": 0, "limit": 16}, context(tmp_path)
    )
    assert page.ok and "needle" in page.stdout
    assert json.loads(page.stderr)["next_offset"] == 16


async def test_exact_size_boundary_and_old_match_shape_are_preserved(tmp_path):
    (tmp_path / "eligible.txt").write_bytes(b"x" * (1024 * 1024 - 7) + b"\nneedle")
    result = await SearchFilesTool().run({"query": "needle"}, context(tmp_path))
    assert result.ok and not result.truncated
    assert json.loads(result.stdout) == [{"path": "eligible.txt", "line": 2, "text": "needle"}]
    coverage = json.loads(result.stderr)
    assert coverage["files_started"] == 1
    assert coverage["observed_skipped_large_files"] == 0
    assert coverage["walk_exhausted"] and not coverage["output_truncated"]


async def test_output_limit_does_not_claim_the_walk_was_exhausted(tmp_path):
    (tmp_path / "matches.txt").write_text("needle " + "x" * 800 + "\n", encoding="utf-8")
    with (tmp_path / "matches.txt").open("a", encoding="utf-8") as stream:
        stream.write(("needle " + "x" * 800 + "\n") * 100)
    result = await SearchFilesTool().run({"query": "needle"}, context(tmp_path))
    assert result.ok and result.truncated
    assert 0 < len(json.loads(result.stdout)) < 101
    coverage = json.loads(result.stderr)
    assert coverage["output_truncated"] and not coverage["walk_exhausted"]
    assert coverage["files_started"] == 1


async def test_skipped_path_sample_is_bounded_without_hiding_observed_count(tmp_path):
    for index in range(12):
        with (tmp_path / f"archive-{index:02}.txt").open("wb") as stream:
            stream.seek(1024 * 1024)
            stream.write(b"x")
    (tmp_path / "small.txt").write_text("needle", encoding="utf-8")
    result = await SearchFilesTool().run({"query": "needle"}, context(tmp_path))
    assert result.ok and json.loads(result.stdout)[0]["path"] == "small.txt"
    coverage = json.loads(result.stderr)
    assert coverage["observed_skipped_large_files"] == 12
    assert len(coverage["skipped_large_paths"]) == 8
    assert coverage["skipped_large_paths_truncated"] and coverage["walk_exhausted"]
    assert len(result.stderr.encode("utf-8")) < 3000


async def test_multibyte_skipped_path_samples_obey_utf8_byte_budget(tmp_path):
    for index in range(10):
        with (tmp_path / ("中" * 90 + f"-{index}.txt")).open("wb") as stream:
            stream.seek(1024 * 1024)
            stream.write(b"x")
    result = await SearchFilesTool().run({"query": "needle"}, context(tmp_path))
    coverage = json.loads(result.stderr)
    paths = coverage["skipped_large_paths"]
    assert result.ok and json.loads(result.stdout) == []
    assert coverage["observed_skipped_large_files"] == 10
    assert 0 < len(paths) < 8
    assert len(json.dumps(paths, ensure_ascii=False).encode("utf-8")) <= 2048
    assert coverage["skipped_large_paths_truncated"]
    assert all((tmp_path / path).is_file() for path in paths)
    assert "\ufffd" not in result.stderr


async def test_oversized_single_match_is_not_reported_as_an_exhaustive_negative(tmp_path):
    (tmp_path / "one-line.txt").write_text("needle" + "中" * 4000, encoding="utf-8")
    result = await SearchFilesTool().run({"query": "needle"}, context(tmp_path))
    assert result.ok and json.loads(result.stdout) == [] and result.truncated
    coverage = json.loads(result.stderr)
    assert coverage["output_truncated"] and not coverage["walk_exhausted"]
    assert coverage["files_started"] == 1
