# P1 — Execution and tools

[简体中文](P1-执行与工具层.md) · [English](P1-execution-and-tools.en.md)

P1 provides file operations, code execution, web retrieval and Git capabilities through
the shared Tool and WorkspaceProvider contracts. The course-required capabilities are
fixed; backend layout, search provider and workspace implementation can change through
coordinated interface decisions.

This brief describes the current checkout and handoff requirements. Available source
code does not certify current regression results, a teammate's sign-off or instructor
task quality.

## Responsibilities and implementation

| Area | Current files | Responsibility |
| --- | --- | --- |
| File tools | `src/moumou/tools/filesystem.py` | Relative-path UTF-8 file operations and declared artifacts |
| Code execution | `src/moumou/tools/execution.py`, `src/moumou/sandbox/process.py` | Commands/Python, output bounds, timeout and cancellation cleanup |
| Path checks | `src/moumou/sandbox/local.py` | Resolve paths and reject workspace escapes/protected metadata |
| Windows processes | `src/moumou/sandbox/winjob.py` | Native Windows process-lifetime support |
| Git operations | `src/moumou/tools/git.py` | Structured local Git and separately classified remote push |
| Search | `src/moumou/tools/search.py` | Configured Tavily retrieval with structured results |
| Workspaces | `src/moumou/adapters/workspace.py`, `src/moumou/workspace/git.py` | Local directory provider and available GitWorkspaces backend |

The default Engine uses LocalDirs. The formal team adapter wraps its provider with
stable private role directories across sequential tasks. GitWorkspaces is an available
injected backend; its presence does not mean every formal run uses Git worktrees.
When used with P3, the optional `register_private_evidence` hook records exact context
and dialogue filenames in local Git metadata. It preserves ordinary dirty-worktree
rejection, evidence readability and artifact isolation; it never stages or commits
private files. Unregistered files remain visible to the existing cleanliness check.
Worktrees containing private evidence are retained instead of silently removed.

Full ancestor-goal records also belong to this private framework archive, not delivered
files. References come only from the validated task-parent chain. Parent-private
answers, approvals, memories and files remain isolated; artifact integration,
dirty-worktree protection and action approval are not relaxed by goal references.

## Registered capabilities

The actual names in `default_tools()` are:

| Capability | Tool names | Approval category |
| --- | --- | --- |
| Read, list and search files | `read_file`, `list_files`, `search_files` | `fs_read` |
| Create/overwrite/append and replace text | `write_file`, `apply_patch` | `fs_write` |
| Delete a file or explicit recursive directory | `delete_file` | `fs_delete` |
| Execute commands and Python | `shell`, `run_python` | `shell` |
| Local Git status/diff/add/commit/log/branch/merge | `git` | `git_commit` |
| Named branch push to a configured remote | `git_push` | `git_push` |
| Web search with title/URL/snippet results | `web_search` | `network` |

The registry also includes `echo` and the orchestration control names
`ask_human`, `submit_result`, `submit_revision` and `submit_plan`.
Control calls are handled by the orchestration layer; they are not independent shell
tools. Document outputs use Markdown/plain text; proprietary document parsers are not
part of the required delivery.

Formal evaluation separately registers P3's `source_history` (`fs_read`), `read_source`
(`network`) and `verify_source_quote` (`fs_read`); these are not implicit additions to
the `default_tools()` inventory above. After team configuration, the verifier binds
once to actual provider-returned role paths, accommodating LocalDirs or managed Git
worktrees without guessing prefixes. Models cannot select paths/stores or paste a
source body. It reads only one retained `metadata_sha256`, without HTTP, search or
latest-page fallback. Lexical match, non-match and unknown are `true`, `false` and
`null`; tool success is not factual or human acceptance. See [interfaces](../INTERFACES.en.md)
for arguments, code-point coordinates and result semantics.

The framework Gate owns approval policy and tracing. Formal sessions require approval
for Git staging/committing as well as other configured critical operations; a role
cannot grant itself permission by selecting tools. P1 returns the result of an
authorized operation and does not answer business questions or approve quality.

## Shared contracts

Implement `Tool.run(args, ctx) -> ToolResult` and, for a workspace backend,
`allocate`, `release` and `integrate` from WorkspaceProvider. Exact schemas live in
[interfaces](../INTERFACES.en.md), rather than duplicated models in this brief.

- Validate arguments and return structured failures with a readable error.
- Resolve file paths within `ctx.workspace`, including traversal and symlink targets.
- Bound stdout/stderr, set `truncated` and preserve the exit/error information.
- Report relative artifact paths, including relevant deletions.
- On timeout/cancellation, clean up supported child processes before returning.
- Return IntegrateResult with copied paths, conflicts and errors; do not hide conflicts.

Only explicit declared artifacts cross workspace boundaries. Dependencies do not share
entire directories or transitive files. Worker validates `submit_result.files`,
rechecks after acceptance and publishes the whole valid declaration; P1 integration
must preserve the distinction between accepted work and delivery conflicts.

## Native Windows and search

Use an activated Python environment; this project's local demonstration environment
is Conda `nlp`. `run_python` uses the project interpreter. The framework's Python
command compatibility does not translate POSIX shell syntax into native Windows
syntax. Follow [setup](../SETUP.en.md) for reproducible commands and configuration.

`web_search` currently requires `TAVILY_API_KEY`. Formal evaluation wraps it in P3's
scenario search ledger: initially six fresh requests shared by the four roles and
sequential tasks, with separate cache/deduplication counters and explicit additional
grants. The course DeepSeek/HKGAI model access is separate. Another search provider is
a design option requiring the same contract and truthful accounting, not a capability
already delivered by this implementation.

## Isolation and handoff limits

LocalJail is application-level path checking. It is not an operating-system security
sandbox for arbitrary shell/Python code. Process timeout cleanup does not establish
kernel-level isolation, and interactive formal sessions do not promise a universal
hard deadline while awaiting people.

Formal personas use project:role memory identity and continuing private directories.
Generic role@task runtime instances retain the shared project-role policy. Tool and
workspace changes must preserve that identity and explicit artifact transfer.

P0 consumes tools, the approval result and integration reports; P2 displays the actual
decisions/results; P3 records outputs, timing, errors and search evidence. Agreement
on these fields requires real consumer checks.

The actual workspace collection returned by P1's provider is the verifier's trusted
binding input before task submission. Unbound calls, foreign paths and rebinding
reject. Sharing group public sources neither opens private directories nor grants
search allowance. Whitespace-only normalization preserves numbers, dates, case and
punctuation. Neither ancestor references nor lexical checks reuse another task's approval.

## Delivery and validation

Preserve meaningful regression sources and document the exact environment, commands,
results and limits when checks are actually run. The [performance report](../PERFORMANCE.en.md)
records executed checks, subsequent-change validation status and matching source identity;
earlier results cannot be applied to the next version. Contract tests alone do not establish
generated-application quality, teacher goal success or actual consumer-member sign-off.

See [delivery checklist](../DELIVERY.en.md), [formal evaluation](../EVAL.en.md),
[P1 operational notes](../P1-EXECUTION.md) and [P1 handoff](../P1-HANDOFF.md).
The formal task entry is `python -m moumou`; `make unit-test` is the separate framework
regression command. Validation is manual; no bundled Actions workflow is required.
