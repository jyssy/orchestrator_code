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
uv run python cli.py ask "write a Python function that retries an HTTP request 3 times"
```

Expected output:
- `Task type: coding`
- Draft answer from Qwen3-Coder-Next
- Revised answer (if judge found issues) from gpt-oss-120b

**Index your codebase for RAG** (run once, re-run after large changes):
```sh
uv run python cli.py index /Users/jelambeadmin/Documents/access-sysops
```

This embeds all `.py`, `.yml`, `.tf`, `.md`, `.sh`, `.j2` files via Qwen3-Embedding-8B
and stores them in `~/.orchestrator/chroma`. Subsequent `ask` calls will automatically
retrieve relevant chunks and inject them as context.

**CLI reference:**
```sh
uv run python cli.py ask "your prompt"                    # basic ask
uv run python cli.py ask "your prompt" -f path/to/file    # include a file as context
uv run python cli.py ask "your prompt" --no-judge         # skip critique pass (faster)
uv run python cli.py ask "your prompt" --plan             # plan first, then approve
uv run python cli.py index /path/to/dir                   # index a directory
```

---

## Daily usage patterns

### Read-only analysis (no approval needed)
```sh
uv run python cli.py ask "explain the dependency chain between CMS infra and PortalCMS Django" --no-judge
```

### Conservative / ops tasks — plan first, approve before executing
```sh
# Shows: scope, proposed changes, what won't change, required checks, human gates, risks
# Then asks: "Proceed with implementation? [y/N]"
uv run python cli.py ask "add a --limit guard to the warehouse deploy playbook" \
  --plan -f access-sysops/Operations_Warehouse_Infrastructure/ansible/apiserver_playbook.yml
```

Set `PLAN_FIRST=true` in `.env` to make plan-gating the default for every `ask`.

### Safe coding tasks
```sh
uv run python cli.py ask "write a Python helper to parse the warehouse API response envelope"
```

### Large file / whole-repo questions (uses gemma-4-31B-it, 262K context)
```sh
uv run python cli.py ask "summarise all the Ansible roles and what hosts they target" \
  -f access-sysops/Operations_CMS_Infrastructure/ansible/application_playbook.yml
```

---

## Phase 4 — MCP server (VS Code Copilot integration)

The MCP server exposes three tools that VS Code Copilot can call in agent mode:
- `ask_orchestrator` — routes a prompt through the full pipeline and returns the answer
- `plan_task` — generates a scoped plan (scope, changes, checks, human gates, risks) without executing anything
- `index_codebase` — indexes a directory into the RAG vector store

**Recommended workflow in Copilot agent mode:**
1. Call `plan_task` first → review the output
2. If approved, call `ask_orchestrator` to implement

**Start the server** (keep this running in a terminal while coding):
```sh
uv run python mcp_server.py
```

**Register it in VS Code** — the config lives at:
```
~/Library/Application Support/Code/User/mcp.json
```
Contents:
```json
{
  "servers": {
    "orchestrator": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "python", "mcp_server.py"],
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

**Judge pass is slow:** Set `JUDGE_ENABLED=false` in `.env` or pass `--no-judge` to the CLI.

**RAG returns empty context:** Run `uv run python cli.py index` to build the index first.

**Continue.dev models not appearing:** Reload VS Code window (`⇧⌘P` → `Developer: Reload Window`).
