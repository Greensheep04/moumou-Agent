"""Workspace-scoped UTF-8 file tools."""

import asyncio
import json
import os
import shutil

from moumou.contracts import ToolResult
from moumou.sandbox.local import LocalJail
from moumou.sandbox.output import MAX_OUTPUT, clip_bytes, json_output


def _schema(properties, required):
    return {"type": "object", "properties": properties, "required": required}


PATH = {"type": "string"}
TEXT = {"type": "string"}


class FileTool:
    timeout_s = 30.0

    async def run(self, args, ctx):
        try:
            if not isinstance(args, dict):
                raise ValueError("arguments must be an object")
            return await asyncio.to_thread(self.execute, args, LocalJail(ctx.workspace))
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            return ToolResult(ok=False, error=str(exc)[:1024])

    def path(self, args, jail, *, mutable=False):
        return jail.resolve(args.get("path", "."), mutable=mutable)


class ReadFileTool(FileTool):
    name = "read_file"
    description = (
        "Read a workspace UTF-8 file; without offset/limit preserve the limited preview; "
        "for complete content pass offset=0 and follow stderr JSON next_offset until eof=true "
        "(UTF-8 byte offsets, at most 8192 content bytes per page, no added text)."
    )
    action_class = "fs_read"
    params_schema = _schema(
        {
            "path": PATH,
            "offset": {
                "type": "integer",
                "minimum": 0,
                "description": "UTF-8 byte boundary to start at; default 0 in paged mode.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_OUTPUT,
                "description": (
                    "Maximum content bytes per page, default 8192; must fit the next complete "
                    "UTF-8 character. Returned next_offset is the continuation cursor."
                ),
            },
        },
        ["path"],
    )

    def execute(self, args, jail):
        paged = "offset" in args or "limit" in args
        if paged:
            offset, limit = args.get("offset", 0), args.get("limit", MAX_OUTPUT)
            if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
                raise ValueError("offset must be a non-negative integer byte offset")
            if (isinstance(limit, bool) or not isinstance(limit, int)
                    or not 1 <= limit <= MAX_OUTPUT):
                raise ValueError(f"limit must be an integer between 1 and {MAX_OUTPUT}")
        path = self.path(args, jail)
        if not path.is_file():
            raise ValueError(f"no such file: {args.get('path')}")
        with path.open("rb") as stream:
            if paged:
                return self._read_page(stream, offset, limit)
            text, truncated = clip_bytes(stream.read(MAX_OUTPUT + 1))
        return ToolResult(ok=True, stdout=text, truncated=truncated)

    @staticmethod
    def _read_page(stream, offset, limit):
        size = stream.seek(0, os.SEEK_END)
        if offset > size:
            raise ValueError("offset exceeds file size")
        stream.seek(offset)
        raw = stream.read(limit + 1)
        if raw and raw[0] & 0xC0 == 0x80:
            raise ValueError("offset must be at a UTF-8 character boundary")
        payload = raw[:limit]
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            if exc.reason != "unexpected end of data" or len(raw) <= limit:
                raise ValueError("file page is not valid UTF-8") from exc
            payload = payload[:exc.start]
            if not payload:
                raise ValueError("limit is too small for the next UTF-8 character") from exc
            text = payload.decode("utf-8")
        if os.fstat(stream.fileno()).st_size != size:
            raise ValueError("file size changed during read; restart pagination")
        next_offset = offset + len(payload)
        eof = next_offset == size
        metadata = {
            "offset": offset,
            "next_offset": next_offset,
            "limit": limit,
            "bytes_returned": len(payload),
            "file_size": size,
            "eof": eof,
        }
        return ToolResult(
            ok=True, stdout=text, stderr=json.dumps(metadata, sort_keys=True), truncated=not eof,
        )


class WriteFileTool(FileTool):
    name = "write_file"
    description = "Create, overwrite or append a UTF-8 file in the workspace."
    action_class = "fs_write"
    params_schema = _schema(
        {
            "path": PATH,
            "content": TEXT,
            "mode": {"type": "string", "enum": ["overwrite", "append"]},
        },
        ["path", "content"],
    )

    def execute(self, args, jail):
        path = self.path(args, jail, mutable=True)
        content = args.get("content")
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        mode = args.get("mode", "overwrite")
        if mode not in ("overwrite", "append"):
            raise ValueError("mode must be overwrite or append")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a" if mode == "append" else "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
        return ToolResult(ok=True, artifacts=[jail.relative(path)], stdout="file written")


def _walk(jail, path):
    if path.is_file():
        yield path
        return
    if not path.is_dir():
        raise ValueError("no such directory")
    for root, directories, files in os.walk(path, followlinks=False):
        safe = []
        for name in sorted(directories):
            try:
                child = jail.resolve(str(os.path.join(root, name)))
                if not child.is_symlink() and not os.path.islink(os.path.join(root, name)):
                    # Junctions may resolve to another directory inside this jail;
                    # do not descend into reparse points either.
                    if (
                        not getattr(os.lstat(os.path.join(root, name)), "st_file_attributes", 0)
                        & 1024
                    ):
                        safe.append(name)
            except (OSError, ValueError):
                continue
        directories[:] = safe
        for name in sorted(files):
            try:
                yield jail.resolve(str(os.path.join(root, name)))
            except (OSError, ValueError):
                continue


class ListFilesTool(FileTool):
    name = "list_files"
    description = "List relative file paths recursively inside a workspace directory."
    action_class = "fs_read"
    params_schema = _schema({"path": PATH}, [])

    def execute(self, args, jail):
        output, truncated = json_output(
            jail.relative(p) for p in _walk(jail, self.path(args, jail))
        )
        return ToolResult(ok=True, stdout=output, truncated=truncated)


class SearchFilesTool(FileTool):
    name = "search_files"
    description = (
        "Search literal text in workspace UTF-8 files up to 1 MiB each; "
        "stderr JSON reports examined files, skipped large paths and output limits. "
        "Zero matches do not establish absence in skipped or unexamined content."
    )
    action_class = "fs_read"
    params_schema = _schema({"path": PATH, "query": TEXT}, ["query"])

    def execute(self, args, jail):
        query = args.get("query")
        if not isinstance(query, str) or not query:
            raise ValueError("query must be a non-empty string")
        coverage = {
            "scope": "paths visible through the workspace jail",
            "max_file_bytes": 1024 * 1024,
            "files_started": 0,
            "observed_skipped_large_files": 0,
            "skipped_large_paths": [],
            "skipped_large_paths_truncated": False,
            "walk_exhausted": False,
        }
        sample_bytes = 2

        def matches():
            nonlocal sample_bytes
            for path in _walk(jail, self.path(args, jail)):
                if path.stat().st_size > coverage["max_file_bytes"]:
                    coverage["observed_skipped_large_files"] += 1
                    relative = jail.relative(path)
                    name_bytes = len(json.dumps(relative, ensure_ascii=False).encode("utf-8")) + 2
                    if (len(coverage["skipped_large_paths"]) < 8
                            and sample_bytes + name_bytes <= 2048):
                        coverage["skipped_large_paths"].append(relative)
                        sample_bytes += name_bytes
                    else:
                        coverage["skipped_large_paths_truncated"] = True
                    continue
                coverage["files_started"] += 1
                with path.open(encoding="utf-8", errors="replace") as stream:
                    for line, text in enumerate(stream, 1):
                        if query in text:
                            yield {
                                "path": jail.relative(path),
                                "line": line,
                                "text": text.rstrip("\r\n"),
                            }
            coverage["walk_exhausted"] = True

        output, truncated = json_output(matches())
        coverage["output_truncated"] = truncated
        coverage["note"] = (
            "Counts cover visited policy-visible entries only; files_started counts opened scans. "
            "walk_exhausted means iterator exhaustion, not proof that filtered or unreadable "
            "entries were visited. Zero matches do not cover skipped files, an unfinished walk "
            "or omitted entries. Use paged read_file for an exact path."
        )
        return ToolResult(
            ok=True, stdout=output, stderr=json.dumps(coverage, ensure_ascii=False),
            truncated=truncated,
        )


class DeleteFileTool(FileTool):
    name = "delete_file"
    description = "Delete a file or explicitly recursive directory; requires approval."
    action_class = "fs_delete"
    params_schema = _schema(
        {"path": PATH, "recursive": {"type": "boolean", "default": False}}, ["path"]
    )

    def execute(self, args, jail):
        path = self.path(args, jail, mutable=True)
        if not path.exists():
            raise ValueError(f"no such file: {args.get('path')}")
        if not isinstance(args.get("recursive", False), bool):
            raise ValueError("recursive must be a boolean")
        if path.is_dir():
            if not args.get("recursive", False):
                raise ValueError("directory deletion requires recursive=true")
            # Validate every entry before starting deletion; reject nested links
            # escaping the workspace and nested Git management directories.
            for root, dirs, files in os.walk(path, followlinks=False):
                for name in dirs + files:
                    jail.resolve(os.path.join(root, name), mutable=True)
            artifacts = [jail.relative(p) for p in _walk(jail, path)]
            shutil.rmtree(path)
        else:
            artifacts = [jail.relative(path)]
            path.unlink()
        return ToolResult(ok=True, artifacts=artifacts, stdout="deleted")


class ApplyPatchTool(FileTool):
    name = "apply_patch"
    description = "Replace exactly one occurrence of old text in a UTF-8 file."
    action_class = "fs_write"
    params_schema = _schema({"path": PATH, "old": TEXT, "new": TEXT}, ["path", "old", "new"])

    def execute(self, args, jail):
        path = self.path(args, jail, mutable=True)
        old, new = args.get("old"), args.get("new")
        if not isinstance(old, str) or not old or not isinstance(new, str):
            raise ValueError("old must be non-empty text and new must be text")
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("patch input exceeds 1 MiB")
        with path.open(encoding="utf-8", newline="") as stream:
            text = stream.read()
        if text.count(old) != 1:
            raise ValueError("old text must match exactly once")
        with path.open("w", encoding="utf-8", newline="") as stream:
            stream.write(text.replace(old, new, 1))
        return ToolResult(ok=True, artifacts=[jail.relative(path)], stdout="patched")
