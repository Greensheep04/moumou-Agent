# Main/P1 Integration Plan

**Goal:** Merge current remote main into feat/p1-sandbox, preserve both implementations' behavior, validate, and publish the result to main as explicitly requested on 2026-11-08.

**Architecture:** Preserve the existing P1 sandbox, tools, approval gates, Git workspaces and strict acceptance option. Keep main's SQLite trace/Recorder integration and interpreter command construction. Resolve the competing process implementations through P1's bounded process executor, carrying Windows python3 alias handling into that executor.

**Environment:** Existing dedicated moumou-agent Conda environment; committed dependency lockfile; no new environment or dependency.

## Steps

- [x] Fetch current refs; confirm clean P1 branch; run baseline full tests.
- [x] Add integration regressions and demonstrate missing behavior before resolving the merge.
- [x] Merge origin/main without committing; resolve builtin.py and integrate Windows command handling in sandbox/process.py.
- [x] Run focused integration tests, full pytest, Ruff and offline P1 demo; inspect semantic merge and obtain independent code review.
- [ ] Commit merge and validation evidence, fetch again, incorporate concurrent changes if needed, then push to main without force. Check resulting remote SHA and Windows/Ubuntu CI.

## Review focus

- Preserve P1 Job Object containment, bounded output and descendant cleanup rather than reviving the superseded reference process implementation.
- Windows python3 aliases must work in compound shell commands without replacing quoted source literals or persisting aliases in workspaces.
- pytest acceptance must use the active interpreter and quote paths containing spaces.
- Git workspace delivery and acceptance evidence must survive SQLite persistence and reopening.
- Recorder projection failure must retain JSONL evidence and recover on reopening.
- Retain all history from both branches; do not commit .env, runs, credentials or unrelated local files.

## Execution notes

The user explicitly authorized integration and push to main. Work proceeds in the clean P1 checkout, preserving branch history with a merge commit. Existing historical P1 documents describe earlier main-merge restrictions; the current user instruction supersedes them.

Local validation complete: 301 full-suite tests, 25 supplemental trace-store tests, Ruff, offline P1 demo, and independent review. Publication follows this merge commit; remote SHA and CI status are reported in the delivery message.
