"""Workspace-scoped UTF-8 file tools."""

import asyncio
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
    description = "Read a UTF-8 file within the workspace; output is limited."
    action_class = "fs_read"
    params_schema = _schema({"path": PATH}, ["path"])

    def execute(self, args, jail):
        path = self.path(args, jail)
        if not path.is_file():
            raise ValueError(f"no such file: {args.get('path')}")
        with path.open("rb") as stream:
            text, truncated = clip_bytes(stream.read(MAX_OUTPUT + 1))
        return ToolResult(ok=True, stdout=text, truncated=truncated)


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
    description = "Search literal text in UTF-8 files up to 1 MiB each."
    action_class = "fs_read"
    params_schema = _schema({"path": PATH, "query": TEXT}, ["query"])

    def execute(self, args, jail):
        query = args.get("query")
        if not isinstance(query, str) or not query:
            raise ValueError("query must be a non-empty string")

        def matches():
            for path in _walk(jail, self.path(args, jail)):
                if path.stat().st_size > 1024 * 1024:
                    continue
                with path.open(encoding="utf-8", errors="replace") as stream:
                    for line, text in enumerate(stream, 1):
                        if query in text:
                            yield {
                                "path": jail.relative(path),
                                "line": line,
                                "text": text.rstrip("\r\n"),
                            }

        output, truncated = json_output(matches())
        return ToolResult(ok=True, stdout=output, truncated=truncated)


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
