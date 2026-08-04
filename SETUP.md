# Agentic Coding Environment — Setup Runbook

Architecture: Local Ollama router (qwen2.5:1.5b) + REALMS specialist models
(Qwen3-Coder-Next / gemma-4-31B-it / gpt-oss-120b) + ChromaDB RAG + MCP server for VS Code.

---

## Prerequisites

- Apple M2, 16 GB unified memory
- macOS with Homebrew installed
- `uv` installed (`brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`)
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

# Test the CLI
uv run orchestrate ask "write a Python function that retries an HTTP request 3 times"
```

Expected output:
- `Task type: coding`
- Draft answer from Qwen3-Coder-Next
- Revised answer (if judge found issues) from gpt-oss-120b

**Audit, then build the RAG index** (run once, re-run after large changes):
```sh
uv run orchestrate audit-index /Users/jelambeadmin/Documents/access-sysops
uv run orchestrate index /Users/jelambeadmin/Documents/access-sysops --rebuild
```

The audit makes no model calls. Indexing prunes generated environments and caches,
honors Git ignores and `.orchestratorignore`, rejects secret-bearing paths and
high-confidence secret content, and only then sends safe chunks to
Qwen3-Embedding-8B. The sanitized index is stored in `~/.orchestrator/chroma`.
`--rebuild` removes stale chunks from older scans.
Rebuild is the safe default. Use `--resume` only after an interrupted run and only
when the source tree has not changed.

Retrieved chunks are labelled with their source and can be restricted to one Git
repository with `--repo-root`. Effective `AGENTS.md` guidance and read-only Git
state are loaded deterministically before RAG context.

## Quick Start — VS Code Copilot workflow

The most common workflow runs in three steps:

```sh
# 1. Change to the repository you want to work in
cd /path/to/your/repo

# 2. Generate the guarded prompt (--repo-root is inferred from the current directory)
uv run orchestrate work "describe the coding change and what done means" --executor copilot

# 3. Copy the printed prompt and paste it into VS Code Copilot agent mode
```

Copilot calls `#plan_task`, shows the plan, and stops without editing. After reviewing it:

> Approved. Implement the plan. Preserve existing unrelated changes.

Copilot then calls `#ask_orchestrator`, implements the approved scope, and reports the handoff.

---

## Command directions and quick reference

Choose the command based on the intended outcome:

| Command | Use it for | Repository changes or side effects |
|---|---|---|
| `ask` | Read-only questions and analysis | No repository edits; may call configured model and RAG services |
| `plan` | A structured, repository-aware plan | No repository edits, approval prompt, or executor launch |
| `work` | A change that may be implemented | Launches the guarded plan → approval → executor workflow |
| `audit-index` | Preview what is safe to index | Read-only safety scan; no model API or Chroma write |
| `index` | Build or update the sanitized RAG index | Writes the local Chroma index and may call the embedding service |

There are two supported ways to invoke the CLI:

```sh
# From /Users/jelambeadmin/Documents/orchestrator_code:
uv run orchestrate --help

# From any directory, including a target repository:
/Users/jelambeadmin/Documents/orchestrator_code/.venv/bin/orchestrate --help
```

Use the absolute executable when working from another repository. The examples
below define a short shell variable only to keep the commands readable:

```sh
ORCHESTRATE_BIN=/Users/jelambeadmin/Documents/orchestrator_code/.venv/bin/orchestrate
```

### Ask: read-only questions and analysis

`ask` does not infer repository context from the current directory. Always pass
`--repo-root` when the answer should include effective `AGENTS.md`, read-only Git
state, and repository-scoped RAG context.

```sh
cd /Users/jelambeadmin/Documents/access-sysops/Operations_ServiceIndex_Infrastructure

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

`ask --plan` does not launch a code executor. Prefer `plan` for plan-only output
and `work` when implementation may follow approval. Setting `PLAN_FIRST=true`
applies the same compatibility plan-and-confirm behavior to every `ask` call.

### Plan: produce a plan without implementation

`plan` infers the current Git repository when `--repo-root` is omitted. Passing
it explicitly is recommended in scripts and documentation.

```sh
cd /Users/jelambeadmin/Documents/access-sysops/Operations_ServiceIndex_Infrastructure

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
to implement it.

### Work: guarded plan, approval, and implementation

`work` infers the current Git repository when `--repo-root` is omitted.

```sh
cd /Users/jelambeadmin/Documents/access-sysops/Operations_ServiceIndex_Infrastructure

# Generate the guarded prompt for VS Code Copilot Agent mode (prints prompt; paste into Copilot)
$ORCHESTRATE_BIN work \
  "describe the coding change and what done means" \
  --executor copilot

# --repo-root is inferred from the current directory; pass it explicitly when needed
$ORCHESTRATE_BIN work \
  "describe the coding change and what done means" \
  --repo-root "$PWD" \
  --executor copilot

# Start the guarded Codex workflow (requires Codex CLI installed)
$ORCHESTRATE_BIN work \
  "describe the coding change and what done means"

# Preview the guarded prompt without launching Codex
$ORCHESTRATE_BIN work \
  "describe the coding change and what done means" \
  --repo-root "$PWD" \
  --print-only
```

### Audit or rebuild the RAG index

Run `audit-index` before `index`. Use `--resume` only for an interrupted scan
whose source has not changed.

```sh
# Read-only safety audit
$ORCHESTRATE_BIN audit-index /Users/jelambeadmin/Documents/access-sysops

# Sanitized full rebuild (safe default)
$ORCHESTRATE_BIN index /Users/jelambeadmin/Documents/access-sysops --rebuild

# Resume an interrupted scan only when its source is unchanged
$ORCHESTRATE_BIN index /Users/jelambeadmin/Documents/access-sysops --resume
```

### Discover every option

```sh
$ORCHESTRATE_BIN --help
$ORCHESTRATE_BIN ask --help
$ORCHESTRATE_BIN plan --help
$ORCHESTRATE_BIN work --help
$ORCHESTRATE_BIN audit-index --help
$ORCHESTRATE_BIN index --help
```

---

## Guarded implementation workflow details

The command catalog above is the authoritative quick reference. The following
sections explain what happens after invoking `work`. Do not let Codex and
Copilot write to the same repository at the same time.

### VS Code Copilot Agent mode

From the target repository:

```sh
cd /path/to/your/repo
uv run orchestrate work "describe the coding change and what done means" --executor copilot
```

The command prints the guarded prompt — copy it and paste into VS Code Copilot agent mode.
Copilot calls `#plan_task`, shows the plan, and stops without editing. After reviewing it, reply:

> Approved. Implement the plan. Preserve existing unrelated changes.

Copilot calls `#ask_orchestrator` with the same `repo_root`, implements the approved scope,
runs checks permitted by `AGENTS.md`, and reports the handoff.

### Codex CLI (requires Codex CLI on PATH)

Change to the repository you want edited:

```sh
cd /Users/jelambeadmin/Documents/access-sysops/Operations_ServiceIndex_Infrastructure
/Users/jelambeadmin/Documents/orchestrator_code/.venv/bin/orchestrate work \
  "describe the coding change and what done means"
```

The launcher uses the current Git root as `repo_root`, starts Codex with
`workspace-write` sandboxing and on-request approvals, and instructs Codex to:

1. Read the effective `AGENTS.md`.
2. Call `plan_task` with the task and repository.
3. Show the plan and stop without editing.
4. Wait for a separate approval message.

After reviewing the plan, reply in the same Codex session:

> Approved. Implement the plan. Preserve existing unrelated changes.

Codex will call `ask_orchestrator` with the same `repo_root`, implement the
approved scope, run permitted checks, and report the final diff, checks run,
checks not run, failures, assumptions, and risks. It will not commit, push,
deploy, access secrets, or perform human-gated operations without separate
explicit authorization.

To target a repository without changing directories:

```sh
cd /Users/jelambeadmin/Documents/orchestrator_code
uv run orchestrate work \
  "describe the coding change and what done means" \
  --repo-root /Users/jelambeadmin/Documents/access-sysops/Operations_ServiceIndex_Infrastructure
```

To preview the guarded prompt without launching Codex:

```sh
uv run orchestrate work "your task" --repo-root /repo --print-only
```

---

## Phase 4 — MCP server (Codex and VS Code integration)

The MCP server exposes four tools that Codex, VS Code Copilot, and other MCP clients can call:
- `ask_orchestrator` — routes a prompt through the full pipeline and returns the answer; pass `use_judge=false` for a faster single-pass response
- `plan_task` — generates a scoped plan (scope, changes, checks, human gates, risks) without executing anything
- `audit_index` — reports what is safe to index without making model calls
- `index_codebase` — safety-scans and indexes a directory into the RAG vector store

The server is advisory. It does not edit files or run commands. Codex or another
coding agent must apply the response and perform validation.

**Recommended agent workflow:**
1. Start Codex in the target Git repository so its local `AGENTS.md` is active.
2. Call `plan_task` with that repository as `repo_root` → review the output.
3. If approved, call `ask_orchestrator` with the same `repo_root`.
4. Have the calling agent edit the files and run only checks permitted by the
   effective `AGENTS.md`; human-only checks remain explicitly pending.

**Optional diagnostic start:**
```sh
.venv/bin/python mcp_server.py
```

Normally, do not start the server manually. A stdio MCP client starts and owns
the process automatically.

### Register with Codex

Codex CLI, the Codex IDE extension, and the desktop app share the MCP configuration
in `~/.codex/config.toml`:

```toml
[mcp_servers.orchestrator]
command = "/Users/jelambeadmin/Documents/orchestrator_code/.venv/bin/python"
args = ["/Users/jelambeadmin/Documents/orchestrator_code/mcp_server.py"]
cwd = "/Users/jelambeadmin/Documents/orchestrator_code"
enabled = true
required = false
startup_timeout_sec = 30
tool_timeout_sec = 300
enabled_tools = ["plan_task", "ask_orchestrator", "audit_index"]
```

Restart Codex, then verify with `codex mcp list` or `/mcp`. Run Codex in the
repository that it should edit:

```sh
codex -C /Users/jelambeadmin/Documents/access-sysops/Operations_PortalCMS_Django
```

Example prompt:

> Call the orchestrator's plan_task tool first and show me the plan. After I
> approve it, call ask_orchestrator with this repository as repo_root, inspect
> its advice, make the changes in the workspace, and run only checks permitted
> by the effective AGENTS.md. Report prohibited checks as pending. Do not push
> or deploy.

### Register with VS Code Copilot

The config lives at:
```
~/Library/Application Support/Code/User/mcp.json
```
Contents:
```json
{
  "servers": {
    "orchestrator": {
      "type": "stdio",
      "command": "/Users/jelambeadmin/Documents/orchestrator_code/.venv/bin/python",
      "args": ["/Users/jelambeadmin/Documents/orchestrator_code/mcp_server.py"],
      "cwd": "/Users/jelambeadmin/Documents/orchestrator_code"
    }
  }
}
```

**Verify:**
- In VS Code Copilot agent mode, type `#plan_task` or `#ask_orchestrator` — both should appear as available tools
- Try: *"Use plan_task to propose how to refactor the contacts_updater.py file"*

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
