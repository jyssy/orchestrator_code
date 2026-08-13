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

## Phase 1: Critical boundary hardening

This phase offers the largest risk reduction and should be completed first.

### 1. Separate planning from write-capable execution

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

**Likely components**

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

### 6. Replace silent degradation with structured results

**Problem**

Some broad exception handlers convert dependency failures into empty context or
fallback output. This makes an actual failure indistinguishable from a valid
result with no matches.

**Enhancement**

- Define structured results for routing, retrieval, model calls, and judging.
- Distinguish success, degraded success, unavailable dependency, invalid input,
  and internal failure.
- Use narrowly scoped exception handling.
- Add bounded retries with jitter only for retryable failures.
- Add timeouts and simple circuit-breaker behavior for remote services.
- Carry warnings into CLI, MCP, and final handoff output.
- Never include credentials, raw source content, or full prompts in logs.

**Likely components**

- `orchestrator/router.py`
- `orchestrator/rag.py`
- `orchestrator/specialists.py`
- `orchestrator/judge.py`
- `orchestrator/pipeline.py`
- New shared result/error types in `orchestrator/models.py`

**Acceptance criteria**

- An unavailable index is distinguishable from zero retrieval matches.
- Reranker fallback is visible to the caller.
- Model configuration errors are not retried as transient failures.
- No broad exception path silently reports success.

### 7. Make offline and fallback behavior coherent

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
