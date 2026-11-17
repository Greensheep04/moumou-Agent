"""Git worktrees with artifact-only delivery commits and recoverable integration."""

import hashlib
import os
import re
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


def run_git(path, *args, data=None, env=None, check=True, binary_output=False):
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
    if not binary_output:
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
            evidence = managed / ".evaluation"
            if evidence.exists() or evidence.is_symlink():
                # Git can remove ignored files with a clean worktree. Evidence
                # registered below must not disappear as a cleanup side effect.
                raise ValueError("workspace contains private evidence; retain it before release")
            # Keep branches and delivery history as evidence; reject dirty trees.
            common = Path(
                self.git(
                    managed, "rev-parse", "--path-format=absolute", "--git-common-dir"
                ).stdout.strip()
            )
            self.git(common.parent, "worktree", "remove", str(managed))

    def register_private_evidence(self, workspace, relative):
        """Exclude one validated P3 evidence file, never the whole private directory.

        This optional framework hook preserves the normal Git dirty-tree check.
        Rules live only in this managed repository's info/exclude, not project
        .gitignore or global configuration. Tracked evidence is not auto-untracked.
        """
        with self._lock:
            managed = self._managed(workspace)
            if not isinstance(relative, str) or not re.fullmatch(
                r"\.evaluation/(?:context/[0-9a-f]{32}\.json|prior-dialogue\.txt)", relative,
            ):
                raise ValueError("unsupported private evidence path")
            target = LocalJail(managed).resolve(relative)
            if target != managed / relative or not target.is_file():
                raise ValueError("private evidence must be an existing file without path aliases")
            if relative.startswith(".evaluation/context/"):
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
                if target.stem != digest[:32]:
                    raise ValueError("private context evidence digest does not match its filename")
            if self.git(managed, "ls-files", "--", relative).stdout.strip():
                raise ValueError(
                    "private evidence is tracked; explicit repository repair is required"
                )
            common = Path(self.git(
                managed, "rev-parse", "--path-format=absolute", "--git-common-dir",
            ).stdout.strip()).resolve()
            exclude = common / "info" / "exclude"
            if exclude.resolve() != exclude or (exclude.exists() and not exclude.is_file()):
                raise PermissionError(
                    "private evidence exclude path is not a regular metadata file"
                )
            exclude.parent.mkdir(parents=True, exist_ok=True)
            original = exclude.read_bytes() if exclude.exists() else b""
            rule = ("/" + relative).encode("ascii")
            if rule not in original.splitlines():
                with exclude.open("ab") as stream:
                    if original and not original.endswith(b"\n"):
                        stream.write(b"\n")
                    stream.write(rule + b"\n")

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

    def _ref(self, path, name, fallback):
        result = self.git(path, "rev-parse", "--verify", name, check=False)
        return result.stdout.strip() if result.returncode == 0 else fallback

    def _input_ref(self, path):
        return "refs/p1/inputs/" + identity(str(path))

    def _tree(self, repo, base, entries):
        index = self.root / (".index-" + uuid.uuid4().hex)
        env = {"GIT_INDEX_FILE": str(index)}
        try:
            self.git(repo, "read-tree", base, env=env)
            for rel, blob in entries.items():
                if blob is None:
                    self.git(repo, "update-index", "--force-remove", "--", rel, env=env)
                else:
                    mode, oid = blob
                    self.git(repo, "update-index", "--add", "--cacheinfo", mode, oid, rel, env=env)
            return self.git(repo, "write-tree", env=env).stdout.strip()
        finally:
            index.unlink(missing_ok=True)
            Path(str(index) + ".lock").unlink(missing_ok=True)

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
            path = source_jail.resolve_artifact(rel)
            normalized = source_jail.relative(path)
            dest_jail.resolve_artifact(normalized)
            if path.is_dir():
                raise ValueError("artifacts must name files, not directories")
            if path.is_file():
                oid = self.git(
                    src, "hash-object", "-w", "--no-filters", "--", str(path)
                ).stdout.strip()
                mode = "100755" if os.name != "nt" and os.access(path, os.X_OK) else "100644"
                entries[normalized] = (mode, oid)
            else:
                entries[normalized] = None
        if self.git(dst, "status", "--porcelain").stdout.strip():
            raise ValueError("destination workspace is dirty; commit or resolve changes first")
        if self.git(dst, "rev-parse", "--verify", "MERGE_HEAD", check=False).returncode == 0:
            raise ValueError("destination already has an unfinished merge")

        baseline = self.git(src, "rev-parse", "refs/p1/baseline").stdout.strip()
        source_input = self._ref(src, self._input_ref(src), baseline)
        target_input = self._ref(dst, self._input_ref(dst), baseline)
        delivery_tree = self._tree(src, source_input, entries)
        delivery = self.git(
            src,
            "commit-tree",
            delivery_tree,
            "-p",
            source_input,
            data=b"P1 declared artifact delivery\n",
        ).stdout.strip()
        old_head = self.git(dst, "rev-parse", "HEAD").stdout.strip()
        index = self.root / (".index-" + uuid.uuid4().hex)
        env = {"GIT_INDEX_FILE": str(index)}
        refs = {}
        try:
            self.git(dst, "read-tree", old_head, env=env)
            for rel in entries:
                pair_ref = "refs/p1/delivered/" + identity(str(src), str(dst), rel)
                base = self._ref(src, pair_ref, source_input)
                patch = self.git(
                    src,
                    "--literal-pathspecs",
                    "diff",
                    "--binary",
                    "--full-index",
                    "--no-renames",
                    "--no-ext-diff",
                    "--no-textconv",
                    base,
                    delivery,
                    "--",
                    rel,
                    binary_output=True,
                ).stdout
                if patch:
                    applied = self.git(
                        dst,
                        "apply",
                        "--cached",
                        "--3way",
                        "--whitespace=nowarn",
                        "-",
                        data=patch,
                        env=env,
                        check=False,
                    )
                    if applied.returncode:
                        conflicts = self.git(
                            dst, "diff", "--name-only", "--diff-filter=U", "-z", env=env
                        ).stdout
                        paths = sorted(filter(None, conflicts.split("\0"))) or [rel]
                        # All conflict stages live only in the disposable index.
                        return IntegrateResult(ok=False, conflicts=paths, error="merge_conflict")
                refs[pair_ref] = delivery
            merged_tree = self.git(dst, "write-tree", env=env).stdout.strip()
        finally:
            index.unlink(missing_ok=True)
            Path(str(index) + ".lock").unlink(missing_ok=True)

        input_tree = self._tree(dst, target_input, entries)
        input_commit = self.git(
            dst,
            "commit-tree",
            input_tree,
            "-p",
            target_input,
            data=b"P1 imported artifact baseline\n",
        ).stdout.strip()
        refs[self._input_ref(dst)] = input_commit
        old_tree = self.git(dst, "rev-parse", old_head + "^{tree}").stdout.strip()
        new_head = old_head
        if merged_tree != old_tree:
            new_head = self.git(
                dst,
                "commit-tree",
                merged_tree,
                "-p",
                old_head,
                "-p",
                delivery,
                data=b"Merge P1 declared artifacts\n",
            ).stdout.strip()
            # Destination cleanliness was checked above. Materialize the already
            # verified index tree; do not run merge drivers in the real index.
            self.git(dst, "read-tree", "--reset", "-u", new_head)
        commands = [f"update {name} {value}" for name, value in refs.items()]
        if new_head != old_head:
            commands.append(f"update HEAD {new_head} {old_head}")
        try:
            self.git(dst, "update-ref", "--stdin", data=("\n".join(commands) + "\n").encode())
        except (OSError, ValueError, subprocess.SubprocessError):
            if new_head != old_head:
                self.git(dst, "read-tree", "--reset", "-u", old_head)
            raise
        return IntegrateResult(ok=True, copied=list(entries))
