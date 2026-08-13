# Enforced Plan–Approval Workflow

Phase 1 adds an enforceable lifecycle for code-executor sessions while keeping
the orchestrator advisory. Planning and execution are separate processes:

```text
read-only plan -> review -> approval record -> state validation
               -> write-capable executor -> changed-path validation
```

The workflow binds approval to the exact task, repository root, base commit,
working-tree fingerprint, effective policy fingerprint, allowed paths, and plan
proposal. Approval records are single-use.

## Create a structured plan

Run `plan` with one or more repository-relative `--allow` values:

```sh
orchestrate plan \
  "add approval validation" \
  --repo-root /path/to/repository \
  --allow 'orchestrator/**' \
  --allow 'tests/**'
```

The command performs repository inspection and model planning without editing
the target repository. It stores the structured plan under the user-local
orchestrator state directory and prints the exact path. Without `--allow`, the
command retains its compatibility behavior and prints a Markdown proposal
without creating an executable plan record.

Allowed paths must be relative to the repository. Absolute paths, Windows drive
paths, parent traversal, null bytes, and `.git/**` are rejected. A pattern ending
in `/**` includes that directory and all descendants; a trailing slash is
normalized to the same form. Other patterns use `PurePosixPath.match()`
semantics, so review the recorded allowlist before approval.

## Review and approve

Review both the displayed proposal and the JSON plan record. Then explicitly
approve that file:

```sh
orchestrate approve /path/printed/by/plan.json --approved-by reviewer-name
```

Omit `--approved-by` to use the current operating-system user. The command
first confirms that repository and policy state still match the plan, then
stores a private approval record outside the target repository.

Running `approve` is the human authorization event. It does not edit the target
repository or launch an executor.

## Validate and execute

Pass both records to `execute`:

```sh
orchestrate execute \
  /path/to/plan.json \
  /path/to/approval.json \
  --executor codex
```

Before starting a write-capable process, the command validates:

- Plan and approval schema versions and content-derived identifiers.
- Exact task, repository, base commit, and initial working-tree identity.
- Effective repository guidance and caller-constraint fingerprint.
- Effective repository model-egress policy, including an absent-policy state.
- Whether the approval was previously consumed.

Execution accepts only the canonical approval path printed by `approve`; copied
records are rejected. The approval is consumed immediately before the executor
starts. A failed or interrupted executor does not make the approval reusable;
create and approve a new plan instead.

The execution prompt requires `ask_orchestrator` to receive the same task,
repository root, and sanitized effective constraints used for planning. The
prior plan proposal is treated as advisory evidence, not policy.

After the executor exits, the command rejects commits and reports any newly
Git-visible change outside the approved path patterns. It also uses file
metadata to detect ordinary edits to paths that were already dirty at planning
time. It never reads file contents for this working-tree fingerprint.

Run `execute --print-only` immediately before real execution to validate the
records and preview the exact command without consuming approval or launching
the executor. Then rerun the same command without `--print-only`.

Immediately before a real Codex or Claude launch—and before consuming the
approval—the CLI authorizes the repository classification and scans the fully
assembled initial agent prompt with Gitleaks. Missing scanner, scanner failure,
or a classification other than `remote-approved` blocks launch. `--print-only`
does not transmit a prompt and therefore does not invoke the scanner.

## Recovery behavior

- If planning or approval reports repository or policy drift, generate and
  review a new structured plan. Do not reuse the stale record.
- If execution is interrupted or the executor fails, its approval remains
  consumed. Inspect the repository, then generate and approve a new plan for any
  remaining work.
- If changed-path validation fails, inspect the final repository state manually.
  The orchestrator deliberately performs no automatic rollback.
- If a required executor is unavailable, no write-capable process starts. Fix
  the local installation and revalidate with `--print-only`.

## Runtime records

The default state location is:

```text
~/.orchestrator/workflow/
  plans/
  approvals/
```

Set `ORCHESTRATOR_STATE_DIR` to use another location. Newly created workflow
directories and files use private user permissions. Do not place the state
directory inside a target repository or commit its records.

Treat plan and approval records as potentially sensitive. They contain task
text, repository paths, policy-source paths, approver identity, constraints, and
the model-generated proposal. That proposal may quote or summarize repository
context even though the working-tree fingerprint itself never reads file
contents. Do not publish or transmit these records without reviewing them.

## MCP compatibility

Existing `plan_task` and `ask_orchestrator` tools remain available. Both accept
an optional `effective_constraints` string so clients can propagate the same
sanitized constraints.

`plan_task_structured` returns a versioned JSON plan without writing it.
`validate_plan_approval` validates supplied plan and approval JSON against the
current repository and policy state. The MCP server does not create approvals
or launch a write-capable executor; those remain explicit client or CLI actions.
MCP validation also does not consume an approval. Single-use enforcement belongs
to the canonical approval record used by the CLI `execute` command.

Phase 2B does not change this approval sequence. Existing executors may continue
to call text-returning `ask_orchestrator` exactly as directed. Clients that need
sanitized component status, warnings, or failure codes may additionally call
`ask_orchestrator_structured`. Neither advisory tool creates, consumes, or
expands an approval.

## Compatibility planning command

`orchestrate work` now launches Codex or Claude in read-only planning mode.
Planning sessions cannot be elevated in place. Use `plan --allow`, `approve`,
and `execute` when implementation may follow. Copilot remains a prompt-only
integration and cannot provide the CLI-enforced execution lifecycle. Although
the shared executor enum causes CLI help to list Copilot, `execute` intentionally
rejects it; use Codex or Claude for enforced execution.

## Security boundary and limitations

The phase establishes a technical boundary before write-capable execution: no
executor is launched until a matching, current, unconsumed approval validates.
It also validates Git-visible scope after execution.

This is not an operating-system path sandbox. A write-capable executor can make
an out-of-scope change before post-execution validation detects it, and ignored
files are not included in Git-visible scope checks. The command deliberately
does not attempt an automatic rollback because doing so could destroy unrelated
work. Review the final diff and repository state before integrating changes.

Later phases may add isolated worktrees or a narrower filesystem sandbox for
pre-write path enforcement. Phase 2A now guards orchestrator model calls and the
initial CLI-generated executor prompt, but it cannot scan later independent file
reads by the external executor. Transactional RAG, general model-registry
changes, distributed workflows, and observability remain out of scope.
