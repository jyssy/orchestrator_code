# Directions — Which Command, With What Permissions

This is the day-to-day usage reference: which command to run for a given
outcome, and exactly what repository, read, write, and model-egress
permissions that command carries — especially relevant if you point the
orchestrator at more than one repository.

For installing and registering the orchestrator itself (Ollama, the Python
environment, MCP registration with Zed/Codex/Claude Code or any other
MCP-capable client, hardware and model-routing reference, troubleshooting),
see [`SETUP.md`](SETUP.md). For the
full plan/approval record format and its limitations, see
[`PLAN_APPROVAL_WORKFLOW.md`](PLAN_APPROVAL_WORKFLOW.md). For the model-egress
and secret-scanning boundary, see [`SECURITY.md`](SECURITY.md).

---

## Quick Start — copy/paste guarded workflows

Use `plan`, `approve`, and `execute` for implementation. `--allow` grants write
scope; `--deny` removes write scope and always wins when both match. Neither
option limits what the executor may read inside the target repository.

### Most common: run from inside the target repository

This bounded-broad example lets the executor change application code and tests,
but not infrastructure, CI, or Django migration files:

```sh
cd /path/to/target-repository

orchestrate plan "describe the change" \
  --allow '**' \
  --deny 'infra/**' \
  --deny 'ansible/**' \
  --deny 'terraform/**' \
  --deny '.github/**' \
  --deny '**/migrations/**'

orchestrate approve --latest
orchestrate execute --latest --print-only

# Choose exactly one executor for the real run:
orchestrate execute --latest                    # Codex (default)
# orchestrate execute --latest --executor claude
```

Remove the migrations deny when migration files are intentionally part of the
approved task. The approval is single-use, so do not run both executor lines.

### Pick the write scope appropriate for the task

```sh
# Focused: one Django app and its tests.
orchestrate plan "describe the change" \
  --allow 'customers/**' \
  --allow 'tests/customers/**'

# Bounded broad: anywhere except protected operational areas.
orchestrate plan "describe the change" \
  --allow '**' \
  --deny 'infra/**' \
  --deny '.github/**' \
  --deny '**/migrations/**'

# Repository-wide: use only when all otherwise-permitted paths may be changed.
orchestrate plan "describe the repository-wide change" --allow '**'
```

After any one of those planning commands, the remaining commands are always:

```sh
orchestrate approve --latest
orchestrate execute --latest --print-only
orchestrate execute --latest                    # Codex (default)
# orchestrate execute --latest --executor claude  # choose this instead
```

### Add a sibling repository as read-only context

Put `--add-dir` on `plan` only. It must name the root of another Git repository;
the approved plan carries it through approval and execution automatically:

```sh
cd /path/to/application-repository

orchestrate plan "describe the app change using infrastructure as context" \
  --allow '**' \
  --deny 'infra/**' \
  --deny '.github/**' \
  --add-dir ../access-sysops

orchestrate approve --latest
orchestrate execute --latest --print-only
orchestrate execute --latest --executor claude
```

The executor may read `access-sysops`, but the workflow does not authorize it
to edit that repository.

### Run from anywhere with an explicit repository root

Repeat `--repo-root` on all three commands when you are not inside the target
repository:

```sh
orchestrate plan "describe the change" \
  --repo-root /path/to/target-repository \
  --allow '**' \
  --deny 'infra/**' \
  --deny '.github/**'

orchestrate approve --latest --repo-root /path/to/target-repository
orchestrate execute --latest --repo-root /path/to/target-repository --print-only

# Choose exactly one executor:
orchestrate execute --latest --repo-root /path/to/target-repository
# orchestrate execute --latest --repo-root /path/to/target-repository --executor claude
```

### Use explicit plan and approval files when `--latest` is ambiguous

```sh
orchestrate plan "describe the change" --allow 'src/**' --allow 'tests/**'

# Copy the exact path printed by plan.
PLAN_FILE=/exact/path/printed/by/plan
orchestrate approve "$PLAN_FILE"

# Copy the exact path printed by approve.
APPROVAL_FILE=/exact/path/printed/by/approve
orchestrate execute "$PLAN_FILE" "$APPROVAL_FILE" --print-only
orchestrate execute "$PLAN_FILE" "$APPROVAL_FILE" --executor claude
```

`--deny` and `--add-dir` require a structured `plan --allow` command. Do not
repeat either on `approve` or `execute`; they are bound into the plan. See
[`PLAN_APPROVAL_WORKFLOW.md`](PLAN_APPROVAL_WORKFLOW.md) for record formats,
drift checks, scope validation, and current enforcement limitations.

---

## Command directions and quick reference

Choose the command based on the intended outcome:

| Command | Use it for | Repository changes or side effects |
|---|---|---|
| `ask` | Read-only questions and analysis | No repository edits; may call configured model and RAG services |
| `plan` | A repository-aware plan; `--allow` creates a structured record | No target-repository edits or executor launch |
| `approve` | Explicitly approve an unchanged structured plan | Writes a private user-local approval record |
| `execute` | Launch Codex or Claude for a valid approval | Consumes approval and permits scoped repository edits |
| `work` | Compatibility planning launcher | Launches an agent with read-only permissions |
| `audit-index` | Preview what is safe to index | Read-only safety scan; no model API or Chroma write |
| `index` | Build or update the sanitized RAG index | Writes the local Chroma index and may call the embedding service |
| `policy` | Report every repo's model-egress classification under a directory tree | Read-only; no repository, policy, or Chroma writes |

### Permissions model — what each command can touch, across many repos

There is no global or workspace-wide permission. Every command operates on
**exactly one target repository per invocation**, and every repository carries
its own independent permissions. If you work across many repos, treat each one
as a separate trust decision — nothing here accumulates or inherits across
repos.

Three separate axes decide what a command can actually do. A command's row in
the table below sets all three:

1. **Which repo** — resolved from `--repo-root`, or inferred from your current
   directory, or bound inside a plan/approval record. `ask` never infers; you
   must always pass `--repo-root` explicitly.
2. **Read scope** — which directories the process (or the Codex/Claude
   subprocess it launches) can see. Default is the target repo's own tree.
3. **Write scope** — which paths inside that one repo may actually be edited.
   The guarded executor is configured to write only in the target repo; bound
   context repos are explicitly denied writes and checked again after execution.
4. **Model egress** — whether that repo's content is allowed to leave the
   machine at all, and to where. This is controlled per-repo by that repo's
   own `.orchestrator-policy.toml` (`deny-model` / `local-only` /
   `remote-approved`, default `local-only` if the file is absent) — see
   [`SECURITY.md`](SECURITY.md). It is completely independent of read/write
   scope: a command can be allowed to read and write a repo while still being
   blocked from ever sending that repo's content to a remote model.

| Command | Target repo | Reads | Writes | Model egress |
|---|---|---|---|---|
| `ask` | **Required** `--repo-root`, never inferred | That repo's AGENTS.md, Git state, `--file`, and its RAG index only | Never | Yes, subject to that repo's policy |
| `plan` | `--repo-root`, or current directory if omitted | Same as `ask`; with `--allow`, repeatable `--add-dir` values bind separate read-only context repositories and query their existing RAG entries | Never edits any repo. `--allow` records proposed write scope and `--deny` removes matching paths from that scope; deny always wins | Yes, subject to every participating repo's policy |
| `approve` | Whatever repo is bound in the plan record you pass in — or, with `--latest`, the repo from `--repo-root`/cwd, resolved to that repo's newest plan record | Re-checks the target and every bound context repo's current Git/policy state to confirm nothing drifted | Never | No model calls |
| `execute` | Whatever repo is bound in the plan record — or, with `--latest`, the repo from `--repo-root`/cwd, resolved to that repo's newest unconsumed approval | The launched Codex/Claude subprocess works in the target repo and may inspect context repos stored by `plan --add-dir` | Executor may edit only target paths matching `--allow` and no `--deny`. Claude receives invocation-local deny rules; all executors receive post-execution validation. Context repos remain read-only and are checked after exit | Yes, indirectly; all participating repos must permit remote model use before launch |
| `work` | `--repo-root`, or current directory if omitted | That repo, read-only (Codex `--sandbox read-only` / Claude `--permission-mode plan`), plus any `--add-dir` | Never — a `work` session cannot be elevated into an executor in place | Yes, via `plan_task` calls the launched agent makes |
| `audit-index` | The directory argument you pass | Scans that tree read-only | Never | No model calls |
| `index` | The directory argument you pass | Scans that tree | Writes to the shared `~/.orchestrator/chroma` vector store — not the source repo | Yes, embeddings only, and only accepts remote transmission for repos classified `remote-approved` |

Two things worth calling out explicitly when you have many repos in play:

- **A single plan/approve/execute cycle is scoped to one repo.** There is no
  way to approve a change that writes across two repositories in one cycle —
  run the lifecycle separately per repo.
- **`--deny` always overrides `--allow`.** Both are write-scope controls, not
  read filters. They are normalized, stored in the plan digest, and cannot be
  changed after approval. Claude receives pre-write tool/sandbox deny rules;
  Codex and Claude are both checked after execution. Target path validation is
  still not an automatic rollback boundary.
- **Structured `--add-dir` is approval-bound read context.** Put it on
  `plan --allow`, not `execute`. Each value must resolve to a separate Git repo;
  its state and policy are signed into the plan, rechecked at approval and
  execution, protected with executor-specific write restrictions, and checked
  again after execution. Avoid broad persistent
  `permissions.additionalDirectories`, because Claude's native setting follows
  the active permission mode and is not inherently read-only.
- **Check classification across every repo at once with `orchestrate policy`.**
  It walks a directory tree (default `~/Documents`), finds every independent
  Git repository beneath it — stopping at each repo's own `.git` boundary, so
  submodules aren't double-counted — and prints each one's effective
  classification:

  ```sh
  $ORCHESTRATE_BIN policy                    # scans ~/Documents by default
  $ORCHESTRATE_BIN policy ~/work/other-tree  # or scan any other directory
  ```

  It is read-only: it never edits a policy file, never calls a model, and
  never writes state. A missing `.orchestrator-policy.toml` reports as
  `local-only` (the safe default); a present-but-malformed one reports as
  `invalid` so you can find and fix it instead of assuming it's blocking
  silently. The summary line counts how many of your repos are
  `remote-approved` — the number you actually want to keep small and
  deliberate. To change a repo's classification, still edit that repo's own
  `.orchestrator-policy.toml` by hand (or copy
  [`.orchestrator-policy.example.toml`](.orchestrator-policy.example.toml)
  into it) — `policy` intentionally does not write classifications for you,
  since widening egress is a per-repo security decision, not something to
  bulk-apply.

### Do you need `--repo-root`, or is cwd enough?

`plan`, `work`, `approve --latest`, and `execute --latest` all infer the
target repo from your **current directory** when `--repo-root` is omitted.
So the rule is simple: **if you're `cd`'d into the repo you want to act on,
omit `--repo-root`; if you're anywhere else, you must pass it.** (`ask` is the
one exception — it always requires `--repo-root` explicitly, regardless of
cwd.)

That rule only holds, though, if the command you're typing actually sees your
real cwd. There are two ways to invoke the CLI, and they differ here:

```sh
# 1. uv run — only correct from inside orchestrator_code's own directory.
#    Running this from a target repo fails (uv can't find the project),
#    and running it from orchestrator_code means cwd is orchestrator_code,
#    never your target repo — so --repo-root is effectively always required.
cd /path/to/orchestrator_code
uv run orchestrate plan "..." --repo-root /path/to/target-repo --allow 'src/**'

# 2. The absolute venv executable — works from any directory, including
#    the target repo itself, so cwd-based inference works as expected.
cd /path/to/target-repo
/path/to/orchestrator_code/.venv/bin/orchestrate plan "..." --allow 'src/**'   # no --repo-root needed
```

Note `--allow` in both: without it, `plan` only prints a read-only Markdown
proposal and creates no structured record at all — `approve --latest`
afterward will correctly report "No plan record found," because there's
nothing to find. `--allow` is what makes `plan` write the record that
`approve`/`execute` need.

The examples below define a short shell variable only to keep the commands
readable:

```sh
ORCHESTRATE_BIN=/path/to/orchestrator_code/.venv/bin/orchestrate
```

**Setup already handles this for you going forward.** SETUP.md Phase 2 has
you put the absolute executable on `PATH` once, so plain `orchestrate` from
any shell resolves to the right binary without `uv run` or a full path. If
that step was skipped or you're troubleshooting on an older machine, see
["Put the orchestrator on `PATH` once"](SETUP.md#phase-2--python-orchestrator)
in `SETUP.md`. Once it's on `PATH`, the only thing left deciding whether you
need `--repo-root` is whether your `cd` matches the repo you mean to act on
— never which directory the orchestrator itself lives in.

### Ask: read-only questions and analysis

`ask` does not infer repository context from the current directory. Always pass
`--repo-root` when the answer should include effective `AGENTS.md`, read-only Git
state, and repository-scoped RAG context.

```sh
cd /path/to/target-repository

# Repository-aware question
$ORCHESTRATE_BIN ask \
  "explain how this application is deployed" \
  --repo-root "$PWD"

# Include one safe file from the repository as additional context
$ORCHESTRATE_BIN ask \
  "explain how this inventory controls deployment" \
  --repo-root "$PWD" \
  --file "$PWD/ansible/hosts"

# Skip the critique/revision pass for a faster answer
$ORCHESTRATE_BIN ask \
  "summarize the dependency-management workflow" \
  --repo-root "$PWD" \
  --no-judge

# Compatibility workflow: show a plan, request confirmation, then produce advice
$ORCHESTRATE_BIN ask \
  "describe how to refactor this component" \
  --repo-root "$PWD" \
  --plan
```

`ask --plan` does not launch a code executor. Prefer `plan` for plan-only output,
`work` for an interactive read-only planning agent, and `plan --allow` followed
by `approve` and `execute` when implementation may follow. Setting
`PLAN_FIRST=true` applies the compatibility plan-and-confirm advisory behavior
to every `ask` call.

### Plan: produce a plan without implementation

`plan` infers the current Git repository when `--repo-root` is omitted. Passing
it explicitly is recommended in scripts and documentation.

```sh
cd /path/to/target-repository

# Repository-aware plan
$ORCHESTRATE_BIN plan \
  "describe the proposed coding or operations change" \
  --repo-root "$PWD"

# Include one safe repository file as additional context
$ORCHESTRATE_BIN plan \
  "propose a narrowly scoped inventory change" \
  --repo-root "$PWD" \
  --file "$PWD/ansible/hosts"
```

The command prints the plan and exits. Its output is a proposal, not approval
to implement it. Add one or more `--allow` values to create a structured plan
record suitable for the separate `approve` and `execute` commands. Add repeatable
`--deny` values to subtract protected paths from that scope. A deny match always
wins, regardless of option order.

When the target app needs a sibling infrastructure or shared-library repository
for context, bind it while creating that record:

```sh
$ORCHESTRATE_BIN plan \
  "update the app using the deployed topology as context" \
  --repo-root "$PWD" \
  --allow '**' \
  --deny 'infra/**' \
  --deny '.github/**' \
  --add-dir ../access-sysops
```

The context repo is read-only. Its canonical root, Git state, and effective
policy are stored in the plan, so later commands stay unchanged.

### Approve and execute a structured plan

The `plan --allow` command prints a plan ID, any denied paths, and the exact
plan-record path. Read the proposal and JSON record before continuing. Copy that
path exactly:

```sh
PLAN_FILE=/exact/path/printed/by/plan
$ORCHESTRATE_BIN approve "$PLAN_FILE" --approved-by reviewer-name
```

`approve` rechecks repository and policy state, then prints the canonical
approval-record path. Copy that path exactly; copied approval files are rejected.
Validate immediately before execution:

```sh
APPROVAL_FILE=/exact/path/printed/by/approve
$ORCHESTRATE_BIN execute "$PLAN_FILE" "$APPROVAL_FILE" --print-only

# Remove --print-only only after reviewing the validated command.
# Codex is the default; this is equivalent to omitting --executor.
$ORCHESTRATE_BIN execute "$PLAN_FILE" "$APPROVAL_FILE" --executor codex

# To run Claude Code instead of Codex, pass --executor claude explicitly.
$ORCHESTRATE_BIN execute "$PLAN_FILE" "$APPROVAL_FILE" --executor claude
```

Instead of copying paths, both commands accept `--latest --repo-root <repo>`
(or just `--latest` from inside the repo) to resolve automatically to that
repo's most recent plan / most recent unconsumed approval — never a plan or
approval belonging to a different repository, even if it's newer:

```sh
$ORCHESTRATE_BIN approve --latest --repo-root /path/to/repo --approved-by reviewer-name
$ORCHESTRATE_BIN execute --latest --repo-root /path/to/repo --print-only
$ORCHESTRATE_BIN execute --latest --repo-root /path/to/repo --executor codex
```

`--latest` and an explicit path argument are mutually exclusive. If you have
more than one pending plan for the same repository, `--latest` picks the most
recently created one — pass the explicit path instead when that's not the one
you mean.

If drift is reported, generate and approve a new plan. If execution fails or is
interrupted, the approval stays consumed; inspect the repository before starting
a newly planned and approved attempt. Scope failures are not rolled back.

Plan records can contain model-generated text derived from repository context,
along with task, path, policy-source, constraint, and approver metadata. Review
them before sharing; do not commit them to the target repository.

New plans also bind `.orchestrator-policy.toml` (including its absent state) into
the policy fingerprint. Regenerate Phase 1 plan records after this upgrade.
Changing repository classification after planning invalidates approval.

### Work: compatibility read-only planning

`work` infers the current Git repository when `--repo-root` is omitted.

```sh
cd /path/to/target-repository

# Codex — read-only planning subprocess (default; requires Codex CLI installed)
$ORCHESTRATE_BIN work \
  "describe the coding change and what done means"

# Claude Code — pass --executor claude explicitly to select it over the Codex default
$ORCHESTRATE_BIN work \
  "describe the coding change and what done means" \
  --executor claude

# Claude Code, also permitted to read a directory outside the target repository
$ORCHESTRATE_BIN work \
  "describe the coding change and what done means" \
  --executor claude \
  --add-dir /path/to/shared-library

# Preview the planning prompt without launching the planning agent
$ORCHESTRATE_BIN work \
  "describe the coding change and what done means" \
  --repo-root "$PWD" \
  --print-only
```

The planning process cannot be elevated into execution. Use the structured
`plan --allow`, `approve`, and `execute` lifecycle for implementation.

### Audit, rebuild, or refresh the RAG index

Before indexing anything, confirm the repository is classified
`remote-approved` — `index` silently skips every file from a repo that isn't
(reported as a `model egress local-only` or similar skip count, not an
error). Check with `orchestrate policy /path/to/workspace`; see
["Check classification across many repos"](#check-classification-across-many-repos)
below.

Run `audit-index` before `index` — it's read-only and makes no model calls,
so it's a safe way to see what would happen first:

```sh
# Read-only safety audit
$ORCHESTRATE_BIN audit-index /path/to/workspace
# → Would index 42 chunks from 11 files under /path/to/workspace.
#   Skipped: model egress local-only=8, too large=1
```

There is **one shared index across every repo you've indexed**, not one per
repo, so the three update modes differ in scope, not just in speed:

| Mode | What it deletes first | What it re-embeds | Picks up edits/deletions? | Cost |
|---|---|---|---|---|
| `--rebuild` (default) | The **entire** collection — every repo you've ever indexed | Every accepted file under the path you give it | Yes | Highest — re-embeds everything, every repo, every time |
| `--resume` | Nothing | Only files/chunks whose ID isn't already present | **No** — an edited file keeps serving its old chunk, since chunk IDs are `hash(path + offset)`, not content | Lowest, but silently stale |
| `--refresh` | Only chunks belonging to the repositories found under the path you give it | Every accepted file in those repositories | Yes, within the repos you point it at | Medium — cheaper than `--rebuild` since other repos are untouched, but still re-embeds the whole scanned repo, not just the changed file |

```sh
# Full rebuild — only correct when you point it at your whole workspace
# root (e.g. ~/Documents), since it wipes every other repo's coverage
$ORCHESTRATE_BIN index ~/Documents --rebuild
# → Rebuilt index with 42 chunks from 11 files under /Users/you/Documents;
#   stored 42 new chunks. Skipped: none

# Resume an interrupted scan only when its source is unchanged since —
# NOT an update mechanism; treat it as "continue," not "refresh"
$ORCHESTRATE_BIN index /path/to/workspace --resume

# You edited one repo and want the index to reflect it, without
# rebuilding (and re-paying for) every other repo you've indexed
$ORCHESTRATE_BIN index /path/to/that-one-repo --refresh
# → Updated index with 6 chunks from 3 files under /path/to/that-one-repo;
#   stored 6 new chunks. Skipped: none
```

`--refresh` is the right default for "I changed this repo, keep the index
current" day to day. Reach for a full `--rebuild` of your whole workspace
root only periodically, or after an upgrade that changes the egress-policy
version (see [`SECURITY.md`](SECURITY.md)). None of these three modes are
free of the underlying limitation: chunk IDs aren't content-addressed, so
even `--refresh` re-embeds a whole repository's files rather than only the
lines that actually changed.

All three commands report a `Skipped: reason=count, ...` breakdown so you can
see *why* a file was excluded (policy classification, too large, binary,
sensitive path/content, gitignored, etc.) rather than just a final count.

### Check classification across many repos

`policy` scans a directory tree for every Git repository beneath it and prints
each one's effective `.orchestrator-policy.toml` classification. It defaults to
`~/Documents`, so running it with no arguments is enough if that is where your
repositories live.

```sh
# Scan the default tree (~/Documents)
$ORCHESTRATE_BIN policy

# Scan a different tree
$ORCHESTRATE_BIN policy ~/work
```

It never edits a policy file or calls a model — see
[the permissions section above](#permissions-model--what-each-command-can-touch-across-many-repos)
for what each classification allows.

### Discover every option

```sh
$ORCHESTRATE_BIN --help
$ORCHESTRATE_BIN ask --help
$ORCHESTRATE_BIN plan --help
$ORCHESTRATE_BIN approve --help
$ORCHESTRATE_BIN execute --help
$ORCHESTRATE_BIN work --help
$ORCHESTRATE_BIN audit-index --help
$ORCHESTRATE_BIN index --help
$ORCHESTRATE_BIN policy --help
```

---

## Guarded implementation workflow details

Do not run two executors against the same repository at the same time. Generate
a structured plan with explicit allowed paths, review it, create an approval,
and execute it in a new process. Codex uses `workspace-write` only for the
approved execution process; Claude uses `acceptEdits` only at that stage.

Approval is invalidated if the allowed or denied paths, commit, initial working
tree, or effective policy changes in the target or any approval-bound context
repository. The approval is single-use and is consumed before executor launch.
After execution, the CLI rejects commits and reports Git-visible paths outside
the allowlist or matching the denylist, and rejects detected changes in context
repositories. It does not automatically roll changes back.

See [`PLAN_APPROVAL_WORKFLOW.md`](PLAN_APPROVAL_WORKFLOW.md) for the complete
workflow and its current limitations.

---

## Human gates — what always requires your approval

Consistent with repo-level AGENTS.md conventions across this workspace:

| Action | Gate |
|---|---|
| `terraform apply` / `plan` | Always human-run |
| `ansible-playbook` execution | Always human-run from bastion |
| Database migrations (`migrate`) | Explicit approval + DBA review |
| Git push / merge / tag / release | Explicit approval |
| Vault / credential / secret changes | Explicit approval |
| Production service restarts | Explicit approval |
| Submodule pointer updates | Separately scoped task |

The orchestrator will **propose** these actions in its plan output but will never execute them.
The calling agent must also leave any validation command prohibited by the
effective repository `AGENTS.md` pending for an authorized human.

Keep Git actions separate from implementation. Let the agent use read-only
inspection commands such as `git status`, `git diff`, and `git diff --check`.
After reviewing the handoff, request a local commit explicitly if wanted:

> Create one local commit for the approved changes. Do not push.

Authorize pushes, merges, tags, releases, or deployments as separate actions.
