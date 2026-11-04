"""Git worktrees with artifact-only delivery commits and recoverable integration."""

import hashlib
import os
import subprocess
import threading
import uuid
from pathlib import Path

from moumou.contracts import IntegrateResult
from moumou.sandbox.local import LocalJail


def git_args(path, *args):
    return [
        "git",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "core.autocrlf=false",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "user.name=Moumou Agent",
        "-c",
        "user.email=moumou@localhost",
        "-C",
        str(path),
        *args,
    ]


def run_git(path, *args, data=None, env=None, check=True):
    child_env = dict(os.environ)
    # Never inherit index/worktree redirections from an enclosing Git command.
    for key in list(child_env):
        if key.startswith("GIT_"):
            child_env.pop(key)
    child_env.update(env or {})
    result = subprocess.run(
        git_args(path, *args),
        input=data,
        capture_output=True,
        env=child_env,
        timeout=30,
    )
    result.stdout = result.stdout.decode("utf-8", "replace")
    result.stderr = result.stderr.decode("utf-8", "replace")
    if check and result.returncode:
        raise ValueError(result.stderr.strip()[:1024] or "git command failed")
    return result


def identity(*parts):
    import json

    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:24]


class GitWorkspaces:
    """Synchronous existing WorkspaceProvider port; Git operations are bounded.

    The optional seed is a trusted repository. Agent file tools cannot access
    the provider's shared repository/index metadata.
    """

    def __init__(self, root, seed=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.seed = Path(seed).resolve() if seed is not None else None
        self._lock = threading.RLock()

    def git(self, path, *args, **kwargs):
        return run_git(path, *args, **kwargs)

    def allocate(self, project_id, conversation_id, agent_id):
        with self._lock:
            pool = self.root / ".repos" / identity(project_id, conversation_id)
            path = self.root / "worktrees" / identity(project_id, conversation_id, agent_id)
            if not (pool / ".git").exists():
                pool.parent.mkdir(parents=True, exist_ok=True)
                if self.seed:
                    self.git(
                        pool.parent, "clone", "--no-hardlinks", "--", str(self.seed), str(pool)
                    )
                else:
                    pool.mkdir()
                    self.git(pool, "init", "--initial-branch=main")
                head = self.git(pool, "rev-parse", "--verify", "HEAD", check=False)
                if head.returncode:
                    self.git(pool, "commit", "--allow-empty", "-m", "workspace baseline")
                self.git(
                    pool,
                    "update-ref",
                    "refs/p1/baseline",
                    self.git(pool, "rev-parse", "HEAD").stdout.strip(),
                )
            branch = "agent/" + identity(project_id, conversation_id, agent_id)
            if path.exists():
                self._managed(path)
                return path
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = self.git(pool, "show-ref", "--verify", "refs/heads/" + branch, check=False)
            args = [] if existing.returncode == 0 else ["-b", branch]
            self.git(
                pool,
                "worktree",
                "add",
                *args,
                str(path),
                branch if not args else "refs/p1/baseline",
            )
            return path

    def _managed(self, path):
        resolved = Path(path).resolve(strict=True)
        if resolved.parent != self.root / "worktrees":
            raise PermissionError("workspace is not managed by this provider")
        common = Path(
            self.git(
                resolved, "rev-parse", "--path-format=absolute", "--git-common-dir"
            ).stdout.strip()
        ).resolve()
        if not common.is_relative_to(self.root / ".repos"):
            raise PermissionError("foreign git metadata")
        return resolved

    def release(self, path):
        with self._lock:
            managed = self._managed(path)
            # Keep branches and delivery history as evidence; reject dirty trees.
            common = Path(
                self.git(
                    managed, "rev-parse", "--path-format=absolute", "--git-common-dir"
                ).stdout.strip()
            )
            self.git(common.parent, "worktree", "remove", str(managed))

    def integrate(self, src, dst, artifacts):
        with self._lock:
            try:
                return self._integrate(src, dst, artifacts)
            except (
                OSError,
                ValueError,
                TypeError,
                RuntimeError,
                subprocess.SubprocessError,
            ) as exc:
                return IntegrateResult(ok=False, error=str(exc)[:1024])

    def _integrate(self, src, dst, artifacts):
        src, dst = self._managed(src), self._managed(dst)
        src_common = self.git(
            src, "rev-parse", "--path-format=absolute", "--git-common-dir"
        ).stdout.strip()
        dst_common = self.git(
            dst, "rev-parse", "--path-format=absolute", "--git-common-dir"
        ).stdout.strip()
        if src_common != dst_common:
            raise ValueError("cannot integrate across project/conversation repositories")
        if not isinstance(artifacts, list) or not all(isinstance(p, str) for p in artifacts):
            raise ValueError("artifacts must be a list of relative paths")
        if not artifacts or src == dst:
            return IntegrateResult()
        source_jail, dest_jail = LocalJail(src), LocalJail(dst)
        entries = {}
        for rel in artifacts:
            if Path(rel).is_absolute():
                raise PermissionError("artifact must be relative")
            path = source_jail.resolve(rel, mutable=True)
            normalized = source_jail.relative(path)
            dest_jail.resolve(normalized, mutable=True)
            if path.is_dir():
                raise ValueError("artifacts must name files, not directories")
            entries[normalized] = path
        if self.git(dst, "status", "--porcelain").stdout.strip():
            raise ValueError("destination workspace is dirty; commit or resolve changes first")
        if self.git(dst, "rev-parse", "--verify", "MERGE_HEAD", check=False).returncode == 0:
            raise ValueError("destination already has an unfinished merge")

        delivery_ref = "refs/p1/deliveries/" + identity(str(src), str(dst))
        previous = self.git(src, "rev-parse", "--verify", delivery_ref, check=False)
        base = (
            previous.stdout.strip()
            if previous.returncode == 0
            else self.git(src, "rev-parse", "refs/p1/baseline").stdout.strip()
        )
        index = self.root / (".index-" + uuid.uuid4().hex)
        env = {"GIT_INDEX_FILE": str(index)}
        try:
            self.git(src, "read-tree", base, env=env)
            for rel, path in entries.items():
                if path.is_file():
                    oid = self.git(
                        src, "hash-object", "-w", "--stdin", data=path.read_bytes()
                    ).stdout.strip()
                    mode = "100755" if os.name != "nt" and os.access(path, os.X_OK) else "100644"
                    self.git(src, "update-index", "--add", "--cacheinfo", mode, oid, rel, env=env)
                else:
                    self.git(src, "update-index", "--force-remove", "--", rel, env=env)
            tree = self.git(src, "write-tree", env=env).stdout.strip()
            old_tree = self.git(src, "rev-parse", base + "^{tree}").stdout.strip()
            if tree == old_tree:
                return IntegrateResult(ok=True, copied=list(entries))
            commit = self.git(
                src, "commit-tree", tree, "-p", base, data=b"P1 declared artifact delivery\n"
            ).stdout.strip()
        finally:
            index.unlink(missing_ok=True)
            Path(str(index) + ".lock").unlink(missing_ok=True)

        merged = self.git(
            dst, "merge", "--no-ff", "--no-edit", "--no-gpg-sign", commit, check=False
        )
        if merged.returncode:
            conflicts = self.git(dst, "diff", "--name-only", "--diff-filter=U", "-z").stdout
            paths = sorted(filter(None, conflicts.split("\0")))
            aborted = self.git(dst, "merge", "--abort", check=False)
            if aborted.returncode and paths:
                raise ValueError("merge failed and rollback failed; manual recovery required")
            return IntegrateResult(
                ok=False, conflicts=paths, error="merge_conflict" if paths else merged.stderr[:1024]
            )
        self.git(src, "update-ref", delivery_ref, commit)
        return IntegrateResult(ok=True, copied=list(entries))
