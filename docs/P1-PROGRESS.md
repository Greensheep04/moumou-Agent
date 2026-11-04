# P1 execution ledger
Plan: docs/plans/2026-11-04-p1-execution-and-tools.md
Base: e3b9e7a4c1ab7f486cb16ca7704e957cdc4f1cc9
Branch: feat/p1-sandbox

Ruling: Use the dedicated fresh clone on a feature branch as the isolated development checkout; an extra managed worktree would relocate code away from the user's D:\Projects convention.
Ruling: Keep contracts and P0 orchestration unchanged. Implement synchronous bounded workspace operations to satisfy the existing port; blocking Git latency remains documented.
Ruling: LocalJail is application-level validation only, shell/Python are trusted approved code execution. Docker remains optional; arbitrary-code OS isolation is not claimed.
Ruling: Integration snapshots declared artifacts including deletions into a delivery commit based on a common baseline; no undeclared branch content is merged. Conflicts roll back the destination and are returned as relative paths. Git metadata operations are trusted provider internals.
Ruling: Preserve Windows and POSIX execution support; adapt only test command construction and PID checks where the existing tests assume POSIX. Do not weaken assertions.
Pre-flight: filesystem tools consume LocalJail; process tools consume bounded output helpers; Git tools consume process runner; workspace integration consumes LocalJail and shared Git helpers; default_tools preserves marker tools.
Pending external validation: P0 acceptance of local isolation and synchronous workspace semantics, P3 test-set integration, real GitHub push and real search credentials.
Stage 0: baseline 138 passed / 4 failed (Windows python3 assumptions); logs runs/p1-verification/baseline.txt.
Stage 1: filesystem RED 6 failed / 2 passed, GREEN 18 passed (2 process tests excluded); ordinary file behavior and junction escape covered. Original 4 baseline failures remain for stage 2.
Stage 2: process RED 5 failed / 3 passed; GREEN process+contract 22 passed. Whole suite 160 passed after separating P1 tool limit (110s) from P0 acceptance timeout (unbounded finite positive). Windows Job Objects and POSIX process groups clean descendants. Python3-prefix compatibility keeps existing P0 acceptance code unchanged.
Stage 3: Git/workspace RED 11 failed; GREEN whole suite 171 passed. Artifact-only delivery commits preserve private source files; sequential updates/deletions, parallel delivery, conflict rollback and safe release tested. Windows release invokes Git from the pool rather than the directory being removed.
Stage 4: external-tool RED 11 failed; GREEN 11 passed. Gate approve/reject tested against a local bare remote; Tavily normalization, malformed data, HTTP errors, missing key and bounded JSON tested with MockTransport. Official API contract: https://docs.tavily.com/documentation/api-reference/endpoint/search . No real credentials or network write used.
Stage 5: whole suite 184 passed in 29.23s; ruff check passed. P1 demo: 3 done, 0 dead, script output correct, same-file conflict reported and rolled back. Existing Engine receives tools and worktree provider by dependency injection; P0 contracts and orchestrator files unchanged. CI matrix added but hosted Linux/Windows runs have not been executed here.

Final independent review (base e3b9e7a..431f236): 4 Important findings, no Minor findings. Reviewer read-only. All reviewed source and tests; new prose excluded from review.
Final: fixed dependency provenance and first-delivery deletion semantics — imported/last-delivered per-file refs plus temporary-index three-way patching; dependency/subset/committed-edit regressions RED 4 failures -> GREEN.
Final: fixed inherited/context Git environment redirection — clear GIT_* variables for Git tools and push; context regression RED -> GREEN; inherited case added.
Final: fixed background-child inherited-pipe EOF deadlock — observe root exit independently, terminate descendants before completing drain; regression RED -> GREEN.
Final: fixed Windows pre-Job execution race — suspended launch, assign Job, resume primary thread with documented Windows thread APIs; delayed-assignment side-effect regression RED -> GREEN.
Final: fixed Git merge artifact reporting — before/after fingerprints; regression RED -> GREEN.
Final targeted suite: 30 passed in 53.28s; final full-suite results recorded after completion below.
Ruling: Replace branch-ancestry merging with artifact-specific baseline patching and a merge-parented result commit — reviewed repro showed ordinary dependency edits falsely conflict and deletions can be silently skipped; added Git refs/temporary indexes cost some latency but preserve correctness across artifact subsets.
Final review declined-to-judge dispositions: Linux/hosted CI and real external credentials remain unverified; hostile approved-code OS isolation remains excluded; new docs manually checked against implementation. These are declared validation limits, not verified success claims.
Final full verification: python -m pytest -q -> 193 passed in 72.75s; python -m ruff check src tests -> All checks passed; python -m moumou.demo.p1 -> 3 done / 0 dead, correct output, conflict preserved original destination. Evidence: runs/p1-verification/final-*; committed summary: docs/P1-VALIDATION.md.
Stage 6: usage, handoff, validation report, environment spec, dependency lock and no-key demo delivered. Real service credentials, remote CI/team acceptance and final history screenshots remain pending; no success claimed for those checks.
Deferred minors: none.
