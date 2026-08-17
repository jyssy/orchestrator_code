# Orchestrator Enhancement Roadmap

## Purpose

This roadmap describes how to evolve the orchestrator from a capable internal
prototype into a dependable, security-conscious engineering tool. The goal is
to strengthen the existing architecture rather than replace it.

The current design already provides useful foundations:

- Task classification and specialist-model routing.
- Repository-aware context and effective `AGENTS.md` loading.
- Safety-filtered, repository-scoped retrieval-augmented generation (RAG).
- An optional critique and revision pass.
- CLI and Model Context Protocol (MCP) interfaces.
- A plan, approval, execution, and handoff workflow.

The highest-value improvements are stronger enforcement at trust boundaries,
more reliable indexing and failure handling, and better operational visibility.

## Guiding principles

All enhancements should follow these principles:

1. **Enforce important boundaries in code.** Prompts can communicate policy,
   but authorization, path limits, and approval state should be validated by
   deterministic code.
2. **Keep planning and execution separate.** Planning should be read-only;
   write access should begin only after explicit approval.
3. **Treat repository content as untrusted input.** Source files and retrieved
   text can contain misleading or malicious instructions.
4. **Minimize data exposure.** Only approved, necessary, safety-scanned content
   should be transmitted to remote services.
5. **Fail visibly and safely.** A degraded dependency should produce a clear
   warning, not silently appear to be an empty or successful result.
6. **Preserve the current architecture's simplicity.** Add infrastructure only
   when a demonstrated operational requirement justifies it.
7. **Make behavior testable.** Security and approval rules should have
   deterministic inputs, outputs, and regression tests.

## Recommended target

The practical target is a dependable internal orchestration tool with:

- A technically enforced approval boundary.
- Structured, verifiable plans.
- Consistent policy propagation between architect and executor.
- Transactional, repository-isolated RAG indexes.
- Predictable online, degraded, and offline behavior.
- Content-safe audit events and actionable diagnostics.

This target does not require turning the project into a distributed workflow
platform or an autonomous multi-agent system.

## Status summary (audited August 2026)

An implementation audit against the actual code (not just commit messages)
found roughly **50–55% of this roadmap implemented**, concentrated almost
entirely in Phase 1 and two items of Phase 2. Three commits map cleanly onto
the phases below: `8b27729` (Phase 1), `30924a7` (Phase 2A), `36c7101`
(Phase 2B / item 6). All 64 tests in `tests/` pass. Each item below now
carries a **Current status** line; this table is the fast summary:

| Phase / item | Status | Estimate |
|---|---|---|
| Phase 1, items 1–3 (boundary, structured plans, policy propagation) | Done | ~100% |
| Phase 1, item 4 (workspace roots + remote-indexing consent) | Partial | ~50% |
| Phase 2A (model-egress + secret scanning) | Done | 100% |
| Phase 2, item 5 (transactional RAG) | Not started | ~0% |
| Phase 2, item 6 / Phase 2B (structured results) | Done | 100% |
| Phase 2, item 7 (capability-based model registry, offline mode) | Not started | ~10% |
| Phase 2, item 8 (content-security controls) | Partial | ~75% |
| Phase 2, item 9 (boundary-focused testing) | Partial | ~70% |
| Phase 3, items 10–11 (observability, shared-service controls) | Not started | 0% (deliberately deferred) |

**Highest-impact items still outstanding, in priority order:**

1. **Item 5 — transactional RAG indexing.** `orchestrator/rag.py` still
   deletes the live Chroma collection before new embeddings are confirmed
   (`rebuild=True` path), so an interrupted or failing rebuild can leave the
   index empty or partial — the exact failure mode this item exists to fix.
2. **Item 7 — capability-based model registry / coherent offline mode.** No
   `orchestrator/model_registry.py` exists. Only the coding specialist has a
   local fallback; ops, search, reasoning, judge, embedding, and reranking
   have none, and `OFFLINE_MODE=true` raises a terminal, non-retryable error
   instead of routing to a local provider.
3. **Item 4 — workspace-root allowlist.** No configured boundary restricts
   which directories may be used as `repo_root`/context paths in the CLI or
   MCP. `RAG_SOURCE_DIRS` only widens where `AGENTS.md` is discovered; it is
   not an access-control mechanism.
4. **Item 4 acceptance criterion — index-directory permissions.** Unlike plan
   and approval records (explicitly `chmod(0o700)`/`chmod(0o600)` in
   `orchestrator/approval.py`), the persisted Chroma index directory in
   `orchestrator/rag.py` is created with default permissions.
5. **Item 8 — typed separation of policy vs. untrusted content.** Context is
   still one flattened string with a prepended notice, not distinct
   message roles or typed fields; `how-orchestration-herein-works.md`
   already concedes this does not technically isolate policy from
   repository-controlled content.

**One naming deviation, not a regression:** item 4's proposed `deny-index`
classification shipped as `deny-model` in `orchestrator/security.py` — a
deliberately broader scope (blocks model use generally, not only indexing)
rather than a gap.

## Phase 1: Critical boundary hardening

This phase offers the largest risk reduction and should be completed first.

### 1. Separate planning from write-capable execution

**Current status — Done.** `cli.py` implements `plan`, `approve`, and
`execute` as separate commands. Read-only planning sandboxing lives in
`orchestrator/workflow.py`; approval creation, drift revalidation, and
single-use consumption live in `orchestrator/approval.py`; post-execution
scope validation is `orchestrator/workflow.py`'s `validate_execution_result`,
wired into `cli.py`'s `execute` command. Covered by
`tests/test_phase1_approval.py` (14 tests) and `tests/test_workflow.py`.

**Problem**

The current workflow instructs an executor to stop after presenting a plan, but
the process may already have workspace-write capability. This makes approval a
behavioral convention rather than a technical boundary.

**Enhancement**

- Launch planning in a read-only process or sandbox.
- Capture the repository root, base commit, working-tree state, effective policy
  identity, and proposed scope in the plan.
- Require a separate, explicit approval event.
- Launch a new write-capable execution process only after approval.
- Reject execution if the repository base or approved policy has changed.
- Compare the resulting diff with the approved path scope before handoff.

**Implemented components**

- `orchestrator/workflow.py`
- `orchestrator/pipeline.py`
- `cli.py`
- `mcp_server.py`
- New `orchestrator/approval.py`

**Acceptance criteria**

- The planning process cannot edit the target repository.
- Execution cannot begin without a valid approval record.
- Approval is invalidated by a material repository or policy change.
- Out-of-scope changed paths are reported and block a successful handoff.

### 2. Introduce structured plans

**Current status — Done.** `orchestrator/models.py`'s `StructuredPlan` is
versioned (`SCHEMA_VERSION`), content-addressed (plan ID is a digest of its
contents), and validated on load, rejecting malformed or wrong-version
records. `orchestrator/pipeline.py`'s `plan_structured` builds it from real
repository and policy state. Covered by `tests/test_phase1_approval.py`
(roundtrip/tamper and schema-version-rejection tests).

**Problem**

Free-form Markdown plans are useful to humans but cannot be validated reliably
by software.

**Enhancement**

Define a versioned plan schema containing, at minimum:

- Plan identifier and schema version.
- Original task.
- Canonical repository root.
- Base commit and initial working-tree summary.
- Effective policy fingerprint and policy sources.
- Allowed and prohibited paths.
- Proposed changes.
- Permitted checks and human-only checks.
- Explicit human gates.
- Assumptions and unresolved risks.

Render the human-readable plan from this structure instead of treating model
prose as the authoritative record. Validate model output before presenting it.

**Likely components**

- New `orchestrator/models.py`
- New `orchestrator/approval.py`
- `orchestrator/pipeline.py`
- `mcp_server.py`

**Acceptance criteria**

- Invalid or incomplete plans are rejected with actionable errors.
- Plan records are stable enough to hash and reference during approval.
- The displayed plan and the machine-enforced scope come from the same object.

### 3. Propagate effective policy consistently

**Current status — Done.** `orchestrator/context.py`'s
`load_policy_identity`/`reload_policy_identity` compute one SHA-256
fingerprint over guidance, model-egress policy, and constraints, used
identically by `plan`, `approve`, `execute`, and MCP's
`validate_plan_approval`. Precedence (`AGENTS.override.md` over `AGENTS.md`,
broader over specific) is implemented and tested in `tests/test_context.py`;
fingerprint composition is tested in `tests/test_phase1_approval.py`.

**Problem**

The architect and executor can reach different conclusions if they receive
different policy sources or precedence rules.

**Enhancement**

- Pass sanitized caller constraints to planning and advisory operations.
- Continue loading repository-local guidance deterministically.
- Record every policy source used without recording sensitive content.
- Define and test precedence between global constraints, repository guidance,
  task-specific instructions, and approved scope.
- Include a policy fingerprint in the structured plan.
- Warn when an expected policy source is missing, unreadable, or truncated.

**Likely components**

- `orchestrator/context.py`
- `orchestrator/pipeline.py`
- `orchestrator/workflow.py`
- `mcp_server.py`

**Acceptance criteria**

- Architect and executor report the same effective policy identity.
- More specific policy is applied according to documented precedence.
- Missing policy context is visible rather than silently ignored.

### 4. Restrict accessible roots and remote indexing

**Current status — Partial.** Remote-indexing opt-in via repository
classification is done: `orchestrator/security.py` requires
`DataClassification.REMOTE_APPROVED` from `.orchestrator-policy.toml`,
enforced in `orchestrator/rag.py`. Read-only, no-model-call audit mode is
done (`orchestrator/rag.py`'s `scan_directory`, used by `audit-index` and
`audit_index`). Still missing: no configured **allowlist of workspace
roots** exists anywhere — `RAG_SOURCE_DIRS` only widens where `AGENTS.md` is
discovered, it does not gate which directories may be used as `repo_root` or
context paths in the CLI or MCP; and no **restrictive permissions** are
applied to the persisted Chroma index directory (`orchestrator/rag.py`'s
`mkdir` call has no `mode=`), unlike plan/approval records which are
explicitly `chmod(0o700)`/`chmod(0o600)` in `orchestrator/approval.py`. The
classification names also shipped as `deny-model`/`local-only`/
`remote-approved` rather than the proposed `deny-index` — a deliberately
broader substitution, not a gap.

**Problem**

A caller should not be able to select any readable filesystem directory for
context loading or remote embedding.

**Enhancement**

- Configure an allowlist of workspace roots.
- Canonicalize and validate repository and index paths against that allowlist.
- Require repository-level opt-in before remote embedding.
- Support data classifications such as `deny-index`, `local-only`, and
  `remote-approved`.
- Present a file manifest and safety summary before remote indexing.
- Apply restrictive permissions to locally persisted indexes.

**Likely components**

- `orchestrator/context.py`
- `orchestrator/security.py`
- `orchestrator/rag.py`
- `mcp_server.py`
- Configuration examples and setup documentation

**Acceptance criteria**

- Paths outside configured workspace roots are rejected.
- Remote indexing requires explicit repository authorization.
- Audit mode remains read-only and performs no model or embedding calls.
- Index manifests do not expose source contents or secrets.

## Phase 2: Reliability and assurance

### Phase 2A delivered: model-egress and secret-scanning boundary

The first Phase 2 increment centralizes every Python model-provider call behind
repository classification and mandatory local Gitleaks scanning. It adds
redacted metadata-only failures, scans early context sources and final assembled
payloads, requires explicit remote opt-in, labels repository/RAG content as
untrusted evidence, and rejects legacy RAG chunks without current policy
metadata. See `SECURITY.md`.

This increment intentionally does not implement the transactional index,
general capability registry, distributed workflow, deployment, or observability
items below. It also cannot intercept independent file reads made by external
coding agents; stronger isolation requires a sanitized worktree or OS/container
filesystem boundary.

### 5. Make RAG updates transactional and repository-isolated

**Current status — Not started.** No `orchestrator/index_store.py` exists.
`orchestrator/rag.py`'s `index_directory` (`rebuild=True` path) still calls
`client.delete_collection(...)` before any new embeddings are computed or
uploaded — precisely the failure mode this item exists to fix. There is no
staging collection, manifest (commit/scanner-version/digests), atomic
activation, or retained previous-good version. Self-confirmed as future work
in `how-orchestration-herein-works.md`, `SECURITY.md`, and
`PLAN_APPROVAL_WORKFLOW.md`. This is the single highest-impact item still
outstanding.

**Problem**

Deleting an active collection before a rebuild completes can leave the system
with an empty or partial index. A shared collection also creates avoidable
cross-repository lifecycle coupling.

**Enhancement**

- Build each index in a versioned staging collection.
- Store repository identity, indexed commit, scanner version, and content
  digests in a manifest.
- Validate the staged index before activation.
- Atomically select the new active version.
- Retain the previous known-good version until activation succeeds.
- Isolate indexes by repository identity or namespace.
- Define safe cleanup and retention behavior for superseded versions.

**Likely components**

- `orchestrator/rag.py`
- New `orchestrator/index_store.py`

**Acceptance criteria**

- Interrupted rebuilds leave the previous index usable.
- Rebuilding one repository does not remove another repository's data.
- Retrieval reports the repository and index version used.
- Stale indexes are detected and reported.

### 6. Replace silent degradation with structured results — delivered as Phase 2B

**Current status (August 2026)**

Delivered. The shared contract is implemented in `orchestrator/results.py`,
provider failure classification and bounded retries live in
`orchestrator/model_gateway.py`, and sanitized status propagation now covers
routing, retrieval, reranking, specialist calls, judging, the pipeline, CLI, and
MCP. The original text-returning `ask_orchestrator` contract remains compatible;
`ask_orchestrator_structured` exposes the diagnostic envelope to new clients.

**Problem**

Some broad exception handlers convert dependency failures into empty context or
fallback output. This makes an actual failure indistinguishable from a valid
result with no matches.

**Enhancement**

- Define structured results for routing, retrieval, model calls, and judging.
- Distinguish success, degraded success, unavailable dependency, invalid input,
  invalid configuration, security-policy block, and internal failure.
- Use narrowly scoped exception handling.
- Add bounded exponential retries only for retryable remote failures.
- Carry warnings into CLI, MCP, and final handoff output.
- Never include credentials, raw source content, or full prompts in logs.

**Implemented components**

- `orchestrator/router.py`
- `orchestrator/rag.py`
- `orchestrator/specialists.py`
- `orchestrator/judge.py`
- `orchestrator/pipeline.py`
- `orchestrator/model_gateway.py`
- New shared result/error types in `orchestrator/results.py`
- `cli.py` and `mcp_server.py`

**Acceptance criteria**

- An unavailable index is distinguishable from zero retrieval matches.
- Reranker fallback is visible to the caller.
- Model configuration errors are not retried as transient failures.
- No broad exception path silently reports success.

Circuit breakers, cancellation propagation, and deployment-grade telemetry
remain deliberately deferred; they are separate operational enhancements, not
part of Phase 2B's safe diagnostic contract.

### 7. Make offline and fallback behavior coherent

**Current status — Not started (~10%).** No `orchestrator/model_registry.py`
exists; `orchestrator/specialists.py` hardcodes one model per task-type
category as module constants, with no independently configurable
primary/fallback pairing per capability. Only the coding path has a local
fallback (triggered by `RemoteTransmissionDenied` or
`UNAVAILABLE_DEPENDENCY`) — ops, search, reasoning, judge, embedding, and
reranking have none. `OFFLINE_MODE=true` raises a terminal,
non-retryable `ProviderFailure(INVALID_CONFIGURATION, ...)` rather than
deterministically routing to a local provider, so offline mode is not yet a
coherent, documented capability contract. Self-confirmed in
`how-orchestration-herein-works.md` under "Fallback coverage is
intentionally narrow."

**Problem**

Fallback behavior differs by task type, and offline mode does not provide a
consistent capability contract.

**Enhancement**

- Create a capability-based model registry for routing, coding, operations,
  reasoning, embedding, reranking, and judging.
- Configure primary and fallback providers independently for each capability.
- Select local providers directly in offline mode.
- Distinguish missing configuration from transient provider failure.
- State which capabilities are unavailable or degraded before work begins.
- Allow the judge pass to be skipped explicitly when no safe fallback exists.

**Likely components**

- New `orchestrator/model_registry.py`
- `orchestrator/specialists.py`
- `orchestrator/router.py`
- `orchestrator/judge.py`
- Configuration examples and setup documentation

**Acceptance criteria**

- Offline behavior is deterministic and documented.
- Every pipeline capability has an explicit fallback policy.
- Responses identify degraded operation without exposing sensitive details.

### 8. Continue strengthening content-security controls

**Current status — Partial (~75%).** Done: filename/suffix/directory/content
filters (`orchestrator/security.py`); mandatory Gitleaks scanning (Phase 2A,
`orchestrator/secret_scanner.py`); accept/reject reasons recorded without
content (`orchestrator/rag.py`'s `IndexReport.skipped` counter); local-only
mode for sensitive repositories. Not started: optional entropy-based
detection beyond Gitleaks' own ruleset and the small regex set in
`security.py`. Partial: repository content is prepended with an
untrusted-evidence notice (`orchestrator/pipeline.py`) but policy, caller
intent, and repository content remain **one flattened string**, not
separated into distinct message roles or typed fields —
`how-orchestration-herein-works.md` already concedes "the ordering
communicates priority to the model but does not technically isolate policy
from untrusted repository content."

**Problem**

Filename and known-token detection reduce risk but cannot identify every secret,
confidential artifact, or prompt-injection attempt.

**Enhancement**

- Keep the existing conservative filename, suffix, directory, and content rules.
- Maintain and tune the mandatory Gitleaks integration delivered in Phase 2A.
- Add optional entropy-based detection with tuned false-positive handling.
- Treat repository text and RAG results as quoted evidence, never as policy.
- Keep system policy, user intent, and repository content in clearly separated
  message roles or typed fields.
- Record why each file was accepted or rejected without recording its content.
- Provide a local-only mode for sensitive repositories.

**Likely components**

- `orchestrator/security.py`
- `orchestrator/context.py`
- `orchestrator/rag.py`
- `orchestrator/pipeline.py`

**Acceptance criteria**

- Instructions embedded in repository files cannot override effective policy.
- Secret scanner failures remain fail-closed with redacted metadata-only errors.
- Remote transmission can be disabled independently of local analysis.
- Audit output contains paths and reasons only when policy permits them.

### 9. Expand boundary-focused testing

**Current status — Partial (~70%).** 64 tests across `tests/*.py` currently
pass (`uv run pytest -q`), with strong coverage of policy precedence,
out-of-repo/sensitive explicit files, prompt-injection-adjacent secret
content in tasks, retry exhaustion/timeouts, repo drift between plan and
execute, approval reuse/expiry/mismatch, diffs exceeding approved paths,
redaction guarantees, multi-repo/nested-policy isolation, and stale-RAG-chunk
rejection. Gaps: no transactional-index activation/rollback tests (the
feature itself doesn't exist yet — see item 5), no dedicated
symlink-escape-race test for the approval/execution boundary, and no true
offline-vs-degraded model-selection tests beyond the coding path (see item
7).

The test suite should cover behavior at trust and failure boundaries, including:

- Policy precedence and caller-provided constraints.
- Repository and index paths outside allowed roots.
- Symlink escapes and path replacement races where practical.
- Prompt injection embedded in source or retrieved chunks.
- Partial embedding and persistence failures.
- Transactional index activation and rollback.
- Multi-repository isolation.
- Online, degraded, and offline model selection.
- Timeouts, malformed responses, and retry exhaustion.
- Repository changes between planning and execution.
- Approval reuse, expiry, and policy mismatch.
- Actual diffs that exceed approved paths.
- Redaction and content-free logging guarantees.

Testing should combine unit tests with a smaller set of integration tests using
fake providers and temporary repositories. Live-provider tests should remain
optional and require explicit authorization.

## Phase 3: Operational maturity

Complete this phase only if the orchestrator becomes a shared or business-
critical service.

### 10. Add safe observability

**Current status — Not started.** No structured event emission, request/
plan/execution identifiers, stage timing, or usage counters exist anywhere
in the codebase. `orchestrator/results.py`'s `ComponentResult`/`Diagnostic`
(delivered under item 6) is a reusable foundation but is not yet wired to
any event sink.

Add structured events for:

- Request, plan, approval, and execution identifiers.
- Stage timing and completion status.
- Provider and model identifiers.
- Token or request usage where available.
- Fallback and degraded-mode activation.
- Retrieval source identifiers and chunk counts.
- Policy and index version identifiers.

Do not log credentials, full prompts, source content, embeddings, or unredacted
provider responses by default.

### 11. Add shared-service controls when needed

**Current status — Not started, and deliberately deferred.** No role-based
approval, tamper-evident approval chaining beyond the existing
digest/consumed-marker mechanism, quotas, concurrency limits, cancellation,
health commands, or retention controls exist. This phase is explicitly
gated behind "only if this becomes shared/business-critical" both here and
in `PLAN_APPROVAL_WORKFLOW.md`'s limitations section, so its absence is by
design, not an oversight.

Potential additions include:

- Role-based approval policy.
- Tamper-evident approval records.
- Per-user quotas and concurrency limits.
- Durable cancellation and resumption.
- Health and diagnostics commands.
- Data-retention controls.
- Independent evaluation models for high-risk work.
- Deterministic validators for diffs, schemas, and test results.

These controls are valuable for a shared service but unnecessary overhead for a
single-user local tool.

## Suggested implementation sequence

Implement changes in this order so each step creates a useful security or
reliability improvement on its own:

1. Define structured plan and result schemas.
2. Split read-only planning from write-capable execution.
3. Add approval records, base-state checks, and allowed-path enforcement.
4. Propagate and fingerprint effective policy.
5. Add workspace allowlists and repository indexing consent.
6. Isolate repository indexes and make updates transactional.
7. Replace silent failures with structured degraded states.
8. Introduce the capability-based model registry and coherent offline mode.
9. Harden untrusted-content and secret-scanning boundaries.
10. Expand boundary and failure-injection tests.
11. Add safe observability.
12. Add team controls only when actual usage requires them.

## Delivery strategy

Each enhancement should be delivered as a small, independently reviewable
change. A typical change should include:

- The relevant schema or interface update.
- Deterministic validation logic.
- Unit and integration tests.
- Documentation and migration notes when behavior changes.
- A compatibility path where practical.
- A handoff describing checks run, checks not run, assumptions, failures, and
  unresolved security or deployment risks.

Security-sensitive changes should receive human review before release. Index
migrations, provider changes, and executor-permission changes should be tested
against disposable repositories before use with important source trees.

## What not to add yet

Avoid adding a distributed workflow engine, database-backed task scheduler,
autonomous multi-agent execution, or a large orchestration framework until a
specific requirement demonstrates the need. These additions would increase
deployment, debugging, and security complexity without directly fixing the
current trust-boundary issues.

## Expected outcome

Completing Phase 1 should make the orchestrator substantially safer for regular
internal use. Phase 2 should make failures predictable and protect index
integrity. Phase 3 can make the system suitable for shared operation if adoption
justifies the additional maintenance burden.

The recommended path preserves the project's strongest characteristic: a small,
understandable architecture with useful safeguards. The objective is not maximum
complexity; it is enforceable policy, reliable behavior, and clear evidence of
what the system did.
