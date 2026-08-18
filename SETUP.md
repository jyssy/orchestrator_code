# Agentic Coding Environment — Setup Runbook

Architecture: Local Ollama router (qwen2.5:1.5b) + REALMS specialist models
(Qwen3-Coder-Next / gemma-4-31B-it / gpt-oss-120b) + ChromaDB RAG + an MCP
server any MCP-capable IDE or agent can register (Zed, Claude Code, Codex, ...).

This is installation only: getting Ollama, the Python environment, and MCP
registration in place. It is IDE-agnostic — the orchestrator itself has no
editor dependency, and Phase 3 below registers it once per client using the
same stdio pattern regardless of which editor you use. For which command to
run day to day and what repository/read/write/model-egress permissions it
carries, see [`DIRECTIONS.md`](DIRECTIONS.md).

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

## Phase 2 — Python orchestrator

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

**Put the orchestrator on `PATH` once, so it works from any repository.**
Add this to `~/.zshrc` (or the equivalent for your shell), then open a new
terminal or `source ~/.zshrc`:

```sh
export PATH="/path/to/orchestrator_code/.venv/bin:$PATH"
```

Without this, `orchestrate` only resolves correctly via `uv run orchestrate`
run from inside this directory, or via the absolute
`/path/to/orchestrator_code/.venv/bin/orchestrate` path spelled out every
time. With it on `PATH`, plain `orchestrate` works from any directory,
including the repository you're actually working in — which matters because
most commands infer their target repository from your current directory. See
["Do you need `--repo-root`, or is cwd enough?"](DIRECTIONS.md#do-you-need---repo-root-or-is-cwd-enough)
in `DIRECTIONS.md` for exactly how that inference works.

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

**Build the RAG index once**, to confirm indexing works end to end:
```sh
uv run orchestrate audit-index /path/to/workspace
uv run orchestrate index /path/to/workspace --rebuild
```

Indexing only accepts repositories explicitly classified `remote-approved`, so
point this at the same repository you just authorized above. `--rebuild`
deletes and replaces the **entire** shared index across every repo you've
indexed, not just this one — for the full explanation, the cheaper `--refresh`
mode for updating a single repo, and when to use `--resume`, see
["Audit, rebuild, or refresh the RAG index"](DIRECTIONS.md#audit-rebuild-or-refresh-the-rag-index)
in `DIRECTIONS.md`. This step here is only a smoke test that the pipeline and
ChromaDB are wired up correctly.

Installation is complete once that smoke test and index build succeed. For the
full command reference, the guarded plan/approve/execute workflow, and the
multi-repo permissions model, see [`DIRECTIONS.md`](DIRECTIONS.md).

---

## Phase 3 — MCP server (any MCP-capable client)

The MCP server exposes seven tools that Codex, Claude Code, Zed, and other MCP
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
`execute` lifecycle documented in [`DIRECTIONS.md`](DIRECTIONS.md). It binds
approval to structured records and current repository and policy state.

**Compatibility advisory workflow:** an MCP client may call `plan_task`, stop for
human review, and later call `ask_orchestrator` with the same task, `repo_root`,
and `effective_constraints`. That sequence remains advisory: MCP does not create
or consume the canonical approval record and does not launch the executor.
`ask_orchestrator` still returns the final answer as plain text for existing
clients. Call `ask_orchestrator_structured` when the client must distinguish a
normal result from degraded operation or a sanitized failure.

### The general registration pattern

Every MCP client that supports local stdio servers needs the same two things:
the Python interpreter in this repo's venv (`.venv/bin/python`) and the
absolute path to `mcp_server.py`. Nothing else is required — `mcp_server.py`
loads `.env` from its own directory (`Path(__file__).parent / ".env"`), not
from the client's working directory, so no client needs to set a working
directory or activate the venv itself. That is why each client-specific
section below is short: register the same command using whatever config
format that one client expects.

If your client launches the server without inheriting your interactive shell
— true of most GUI apps, including Zed, which do not source `~/.zshrc` —
rely on `.env` for `REALMS_API_KEY` rather than the shell export.
`mcp_server.py` reads `.env` directly regardless of how it was launched.

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

### Register with Zed

Zed calls MCP servers "context servers." Open Zed's settings with the command
palette action `zed: open settings file` (global settings, typically
`~/.config/zed/settings.json`; a project-level `.zed/settings.json` also
works), or through **Settings → AI → MCP Servers → Add Server → Add Local
Server**:

```json
{
  "context_servers": {
    "orchestrator": {
      "command": "/path/to/orchestrator_code/.venv/bin/python",
      "args": ["/path/to/orchestrator_code/mcp_server.py"],
      "env": {}
    }
  }
}
```

Zed's context-server config has no working-directory field — none is needed
here; see the general pattern above. Prefer setting `REALMS_API_KEY` in
`.env` over Zed's `env` block so the credential isn't duplicated into a
settings file you might sync or share.

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
reads by Claude, Codex, Zed, or other editor extensions. Use a sanitized
worktree or OS/container file boundary where those tools must be unable to
access secret-bearing files.

---

## Memory budget reference (M2 16 GB)

| What's running | Memory used |
|---|---|
| macOS + editor (Zed/VS Code) + Chrome | ~4–5 GB |
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
then run `orchestrate audit-index /path/to/source` followed by
`orchestrate index /path/to/source --rebuild`.

**A context file is refused:** Do not bypass the safety filter. Vault, password,
credential, environment, private-key, and Terraform-state files must remain
outside model context.

**Zed doesn't show the orchestrator's tools:** Confirm the `context_servers`
block in Zed's settings uses absolute paths (not `~`), then reload the window
or restart Zed. Check **Settings → AI → MCP Servers** for a connection error.
