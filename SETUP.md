# Agentic Coding Environment — Setup Runbook

Architecture: Local Ollama router (qwen2.5:1.5b) + REALMS specialist models
(Qwen3-Coder-Next / gemma-4-31B-it / gpt-oss-120b) + ChromaDB RAG + MCP server for VS Code.

---

## Prerequisites

- Apple M2, 16 GB unified memory
- macOS with Homebrew installed
- `uv` installed (`brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`)
- Gitleaks installed locally (`brew install gitleaks`)
- REALMS API key exported in `~/.zshrc`:
  ```sh
  export REALMS_API_KEY="your-key-here"
  ```

---

## Phase 1 — Install Ollama and pull local models

```sh
brew install ollama
```

Start the Ollama background service:
```sh
brew services start ollama
```

Pull the router model (~1 GB):
```sh
ollama pull qwen2.5:1.5b
```

Pull the offline coding fallback (~4.5 GB, optional but recommended):
```sh
ollama pull qwen2.5-coder:7b
```

**Verify:**
```sh
ollama list
# should show qwen2.5:1.5b and qwen2.5-coder:7b

ollama run qwen2.5:1.5b "classify this as one word — coding ops search or general: write a Python function"
# should output: coding
```

---

## Phase 2 — Continue.dev

Install the Continue.dev extension in VS Code:
- Open VS Code → Extensions (⇧⌘X) → search `Continue` → install `Continue (ms-continue.continue)`

The config file is already written at `~/.continue/config.yaml`.
It wires REALMS models (coder, large-context, 120B) and Ollama local models
into Continue's model picker and tab-autocomplete.

**Verify:**
- Open a code file in VS Code
- Press `⌥⌘J` (or click Continue in the sidebar)
- The model picker should show `Qwen3-Coder (REALMS)` and `Gemma 4 31B`
- Tab autocomplete should trigger inline using Qwen3-Coder-Next via REALMS

---

## Phase 3 — Python orchestrator

From this directory (`orchestrator_code/`):

```sh
# Copy and fill in your env file
cp .env.example .env
# Edit .env — set REALMS_API_KEY (can also rely on the shell export)

# Create the uv virtual environment and install dependencies
uv sync

source .venv/bin/activate

# Confirm the mandatory local scanner is available
gitleaks version
```

Before repository content can be sent to REALMS, copy the example policy into
that target repository and explicitly authorize the destination:

```sh
cp /path/to/orchestrator_code/.orchestrator-policy.example.toml \
  /path/to/target-repository/.orchestrator-policy.toml
# Edit classification to remote-approved only after reviewing SECURITY.md.
```

Then test a repository-aware model call:

```sh
uv run orchestrate ask \
  "write a Python function that retries an HTTP request 3 times" \
  --repo-root /path/to/target-repository
```

Missing policy defaults to `local-only`; `deny-model` blocks all model use.
Invalid policy and any unavailable, timed-out, failed, or malformed Gitleaks
result block the model call. See [`SECURITY.md`](SECURITY.md) for the complete
classification and boundary model.

Expected output:
- `Task type: coding`
- Draft answer from Qwen3-Coder-Next
- Revised answer (if judge found issues) from gpt-oss-120b

**Audit, then build the RAG index** (run once, re-run after large changes):
```sh
uv run orchestrate audit-index /path/to/workspace
uv run orchestrate index /path/to/workspace --rebuild
```

The audit makes no model calls. Indexing accepts only repositories explicitly
classified `remote-approved`, prunes generated environments and caches, honors
Git ignores and `.orchestratorignore`, rejects secret-bearing paths and content,
and passes each chunk through Gitleaks before Qwen3-Embedding-8B. The sanitized
index is stored in `~/.orchestrator/chroma`. Phase 2A retrieval rejects legacy
chunks without current egress-policy metadata, so perform one authorized full
`--rebuild` after upgrading. This implementation does not run that rebuild.
Rebuild is the safe default. Use `--resume` only after an interrupted run and only
when the source tree has not changed.

Retrieved chunks are labelled with their source and can be restricted to one Git
repository with `--repo-root`. Effective `AGENTS.md` guidance and read-only Git
state are loaded deterministically before RAG context.

## Quick Start — guarded coding workflow

Two executors are available. Codex is the default for both `work` and
`execute`; call out `--executor claude` explicitly any time Claude Code should
run instead:

| Executor | Command | How it runs |
|---|---|---|
| **Codex** (default) | `orchestrate work "task"` | Read-only planning subprocess |
| **Claude Code** | `orchestrate work "task" --executor claude` | Read-only planning subprocess |

`work` is now the compatibility planning launcher. For an implementation, use
the enforced three-step workflow:

```sh
orchestrate plan "describe the change" --repo-root /path/to/repo --allow 'src/**' --allow 'tests/**'

# Copy the exact Plan record path printed above.
PLAN_FILE=/exact/path/printed/by/plan
orchestrate approve "$PLAN_FILE"

# Copy the exact Approval record path printed by approve.
APPROVAL_FILE=/exact/path/printed/by/approve
orchestrate execute "$PLAN_FILE" "$APPROVAL_FILE" --print-only

# Codex is the default executor; omit --executor to get it.
orchestrate execute "$PLAN_FILE" "$APPROVAL_FILE" --executor codex

# Pass --executor claude explicitly to run Claude Code instead of Codex.
orchestrate execute "$PLAN_FILE" "$APPROVAL_FILE" --executor claude
```

Both `work` and `execute` accept `--executor claude` (short form `-e claude`)
to select Claude Code for that one invocation. The default stays Codex unless
you pass this flag — there is no environment variable or config file override,
so specify it on every command where Claude should run. Add
`--add-dir /path/to/other/directory` (repeatable) on either command to let
that Claude Code session read files outside the target repository, e.g. a
shared library that lives in a sibling directory; it has no effect for Codex.

See [`PLAN_APPROVAL_WORKFLOW.md`](PLAN_APPROVAL_WORKFLOW.md) for record formats,
drift checks, constraints, scope validation, and limitations.

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

There are two supported ways to invoke the CLI:

```sh
# From the orchestrator repository:
uv run orchestrate --help

# From any directory, including a target repository:
/path/to/orchestrator_code/.venv/bin/orchestrate --help
```

Use the absolute executable when working from another repository. The examples
below define a short shell variable only to keep the commands readable:

```sh
ORCHESTRATE_BIN=/path/to/orchestrator_code/.venv/bin/orchestrate
```

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
record suitable for the separate `approve` and `execute` commands.

### Approve and execute a structured plan

The `plan --allow` command prints a plan ID and the exact plan-record path. Read
the proposal and JSON record before continuing. Copy that path exactly:

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

# Let that Claude Code session also read a directory outside the target repo:
$ORCHESTRATE_BIN execute "$PLAN_FILE" "$APPROVAL_FILE" \
  --executor claude \
  --add-dir /path/to/shared-library
```

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

### Audit or rebuild the RAG index

Run `audit-index` before `index`. Use `--resume` only for an interrupted scan
whose source has not changed.

```sh
# Read-only safety audit
$ORCHESTRATE_BIN audit-index /path/to/workspace

# Sanitized full rebuild (safe default)
$ORCHESTRATE_BIN index /path/to/workspace --rebuild

# Resume an interrupted scan only when its source is unchanged
$ORCHESTRATE_BIN index /path/to/workspace --resume
```

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
```

---

## Guarded implementation workflow details

Do not run two executors against the same repository at the same time. Generate
a structured plan with explicit allowed paths, review it, create an approval,
and execute it in a new process. Codex uses `workspace-write` only for the
approved execution process; Claude uses `acceptEdits` only at that stage.

Approval is invalidated if the commit, initial working tree, or effective policy
changes. The approval is single-use and is consumed before executor launch.
After execution, the CLI rejects commits and reports Git-visible paths outside
the allowlist. It does not automatically roll changes back.

See [`PLAN_APPROVAL_WORKFLOW.md`](PLAN_APPROVAL_WORKFLOW.md) for the complete
workflow and its current limitations.

---

## Phase 4 — MCP server (Codex and Claude Code integration)

The MCP server exposes seven tools that Codex, Claude Code, and other MCP
clients can call:

- `ask_orchestrator` — routes a prompt through the full pipeline and returns the answer; pass `use_judge=false` for a faster single-pass response
- `ask_orchestrator_structured` — runs the same pipeline and returns the answer plus sanitized status, component, warning, and error metadata
- `plan_task` — generates a scoped plan (scope, changes, checks, human gates, risks) without executing anything
- `plan_task_structured` — returns a versioned plan record without writing it
- `validate_plan_approval` — checks supplied records against current repository and policy state
- `audit_index` — reports what is safe to index without making model calls
- `index_codebase` — safety-scans and indexes a directory into the RAG vector store

The server is advisory. It does not edit files or run commands. Codex or another
coding agent must apply the response and perform validation.

**Recommended enforced workflow:** use the CLI `plan --allow`, `approve`, and
`execute` lifecycle documented above. It binds approval to structured records
and current repository and policy state.

**Compatibility advisory workflow:** an MCP client may call `plan_task`, stop for
human review, and later call `ask_orchestrator` with the same task, `repo_root`,
and `effective_constraints`. That sequence remains advisory: MCP does not create
or consume the canonical approval record and does not launch the executor.
`ask_orchestrator` still returns the final answer as plain text for existing
clients. Call `ask_orchestrator_structured` when the client must distinguish a
normal result from degraded operation or a sanitized failure.

**Optional diagnostic start:**
```sh
.venv/bin/python mcp_server.py
```

Normally, do not start the server manually. A stdio MCP client starts and owns
the process automatically.

All model-capable MCP calls require Gitleaks. Remote calls also require the
target repository's `.orchestrator-policy.toml` classification to be
`remote-approved`. The policy and scanner block before routing, completion,
embedding, reranking, judging, or revision reaches a provider.

Remote provider calls retry only narrowly classified transient transport, rate
limit, and server failures. The defaults are three attempts with exponential
backoff, configurable with `MODEL_REMOTE_MAX_ATTEMPTS` (1-5) and
`MODEL_RETRY_BASE_SECONDS` (0-5). Scanner findings or failures, policy denial,
authentication failure, invalid requests, and invalid retry configuration are
never retried. Public diagnostics contain fixed status and reason codes; they do
not contain prompts, source chunks, credentials, provider bodies, scanner
output, or unrestricted exception text.

### Register with Codex

Codex CLI, the Codex IDE extension, and the desktop app share the MCP configuration
in `~/.codex/config.toml`:

```toml
[mcp_servers.orchestrator]
command = "/path/to/orchestrator_code/.venv/bin/python"
args = ["/path/to/orchestrator_code/mcp_server.py"]
cwd = "/path/to/orchestrator_code"
enabled = true
required = false
startup_timeout_sec = 30
tool_timeout_sec = 300
enabled_tools = [
  "plan_task",
  "plan_task_structured",
  "validate_plan_approval",
  "ask_orchestrator",
  "ask_orchestrator_structured",
  "audit_index",
]
```

Restart Codex, then verify with `codex mcp list` or `/mcp`. Run Codex in the
repository that it should edit:

```sh
codex -C /path/to/target-repository
```

Example compatibility planning prompt:

> Call the orchestrator's plan_task tool with this repository as repo_root and
> show me the plan. Then stop without editing. I will use the separate structured
> plan, approval, and execute CLI workflow if implementation should follow.

### Register with Claude Code CLI

Claude Code CLI reads MCP config from a JSON file passed via `--mcp-config`.
The orchestrator wires this automatically using `claude-mcp.json` in the
orchestrator root — no manual registration needed for `--executor claude`.

To also have the orchestrator available in interactive `claude` sessions
(started directly without `orchestrate work`), add it to Claude Code's
user-scoped settings (available from any directory):

```sh
claude mcp add --scope user orchestrator \
  /path/to/orchestrator_code/.venv/bin/python \
  /path/to/orchestrator_code/mcp_server.py
```

Note: `claude mcp list` only shows servers in scope for the current directory.
Run it from within a repo where the server is registered to confirm it appears.

**Directory-read permissions.** A Claude Code executor subprocess (from
`work --executor claude` or `execute --executor claude`) is launched with
`cwd` set to the target repository, so its file tools default to that tree.
Two independent knobs extend this:

- Per-launch: pass `--add-dir /path/to/other/directory` (repeatable) to
  `work` or `execute`; it forwards to Claude Code's native `--add-dir` flag
  for that one subprocess only.
- Per-session (interactive `claude`, not the orchestrator subprocess): set
  `permissions.additionalDirectories` in `.claude/settings.json` (project) or
  `~/.claude/settings.json` (global), e.g. `["~/Documents"]` to cover every
  repo under a workspace root without per-project reconfiguration.

**Additional secret protection:** Create a `.claudeignore` at the root of any repo with
sensitive material to block Claude Code's file tools from reading those paths:
```
.env
.env.*
ansible/passwords.yml
ansible/vault*
*.tfstate
*.tfstate.backup
```

This is defense in depth, not enforcement — the orchestrator guards its own
model calls and the initial CLI-generated agent prompt, but not later file
reads by Claude, Codex, Continue, or other editor extensions. Use a sanitized
worktree or OS/container file boundary where those tools must be unable to
access secret-bearing files.

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

---

## Memory budget reference (M2 16 GB)

| What's running | Memory used |
|---|---|
| macOS + VS Code + Chrome | ~4–5 GB |
| qwen2.5:1.5b (router, always resident) | ~1 GB |
| qwen2.5-coder:7b (loaded on demand) | ~4.5 GB |
| ChromaDB + Python process | ~0.5 GB |
| **Total with local coder loaded** | **~10–11 GB** |

Keep at most **one** 7B model loaded at a time. REALMS handles the heavy lifting.

---

## Model routing reference

| Prompt type | Model used | Where |
|---|---|---|
| Coding, debugging, refactoring | Qwen3-Coder-Next | REALMS |
| Ops / Ansible / Terraform | gemma-4-31B-it (262K ctx) | REALMS |
| Complex reasoning, analysis | gpt-oss-120b | REALMS |
| Critique / judge pass | gpt-oss-120b | REALMS |
| RAG embeddings | Qwen3-Embedding-8B | REALMS |
| Quick classification | qwen2.5:1.5b | Local Ollama |
| Offline coding fallback | qwen2.5-coder:7b | Local Ollama |

---

## Troubleshooting

**`REALMS_API_KEY` not found:** Run `source ~/.zshrc` or add to `.env` file.

**Ollama not responding:** Run `brew services restart ollama` or `ollama serve` in a terminal.

**MCP server connects intermittently:** Use the absolute `.venv/bin/python` command
shown above instead of `uv run`, restart the MCP client, and allow at least 30
seconds for startup and 300 seconds for an orchestrator tool call.

**Judge pass is slow:** Set `JUDGE_ENABLED=false` in `.env` or pass `--no-judge` to the CLI.

**RAG returns empty context:** Confirm `repo_root` matches the indexed Git root,
then run `uv run python cli.py audit-index` followed by
`uv run python cli.py index /path/to/source --rebuild`.

**A context file is refused:** Do not bypass the safety filter. Vault, password,
credential, environment, private-key, and Terraform-state files must remain
outside model context.

**Continue.dev models not appearing:** Reload VS Code window (`⇧⌘P` → `Developer: Reload Window`).
