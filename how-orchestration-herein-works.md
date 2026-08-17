# How the Orchestration in This Repository Works

## Purpose

This repository is a Python-based AI orchestration layer. It accepts a task or
question, assembles safe repository context, routes the request to a suitable
language model, optionally adds code retrieved from a local vector index, and can
run a second model pass to critique and revise the answer.

The orchestrator is **advisory**. It produces plans and technical advice, but it
does not edit a target repository or run that repository's validation commands.
A separate coding agent—such as Codex or Claude Code—acts as the
executor after a human approves the plan.

MCP in this repository means **Model Context Protocol**. MCP is the integration
boundary that lets a coding agent discover and call the orchestrator's tools. It
is not a job scheduler, message bus, deployment system, or infrastructure control
plane.

## Current Enhancement Status

As of August 2026, three hardening increments are implemented:

- **Phase 1 — enforced approval lifecycle:** content-addressed plan records,
  single-use approval records, repository/policy drift checks, a separate
  write-capable executor, and post-execution changed-path validation.
- **Phase 2A — model-egress and secret protection:** mandatory local Gitleaks
  scanning, repository egress classifications, a single guarded provider
  gateway, nested-repository policy enforcement, and fail-closed handling.
- **Phase 2B — fail-visible reliability:** typed component outcomes, sanitized
  diagnostics, bounded transient-only remote retries, visible fallback states,
  and additive structured CLI/MCP reporting.

Phase 2B does not change the human approval steps or give the orchestrator write
authority. Transactional RAG, a general model registry, deployment telemetry,
and external-agent filesystem isolation remain future work.

## System at a Glance

```text
Human
  |
  | task and approval
  v
Coding agent (Codex or Claude Code)
  |
  | MCP tool call over stdio
  v
mcp_server.py / FastMCP
  |
  +-- model-egress policy + mandatory local Gitleaks scan
  |                           |
  |                           +-- fail closed before any provider call
  |
  +-- plan_task ----------> pipeline.plan()
  |                           |
  |                           +-- effective AGENTS.md guidance
  |                           +-- read-only Git state
  |                           +-- explicit safe files
  |                           +-- optional RAG results
  |                           +-- reasoning model
  |
  +-- ask_orchestrator ----> pipeline.run()
  |                           |
  |                           +-- classify task
  |                           +-- assemble context
  |                           +-- select specialist model
  |                           +-- generate draft
  |                           +-- critique/revise draft
  |                           +-- plain final text for legacy clients
  |
  +-- ask_orchestrator_structured
  |                        -> same pipeline + sanitized status envelope
  |
  +-- audit_index ---------> safety scan only
  |
  +-- index_codebase ------> scan -> embed -> ChromaDB
  |
  v
Advisory result returned to the coding agent
  |
  +-- agent evaluates the advice
  +-- human approves the proposed work
  +-- agent edits only the approved files
  +-- agent runs only policy-permitted checks
  +-- agent reports the final diff and remaining risks
```

There are three separate authority layers:

| Layer | Responsibility |
| --- | --- |
| Human | Defines the task, reviews the plan, and authorizes implementation or other gated actions. |
| Orchestrator MCP | Supplies repository-aware planning, model routing, retrieval, and review. |
| Coding agent | Inspects the live workspace, evaluates the advice, edits files, and runs permitted checks. |

This separation matters. Model output is not automatically authoritative, even
after the judge pass. The executor must compare it with the actual source and the
effective repository instructions.

## Component Map

| Component | File | Role |
| --- | --- | --- |
| MCP adapter | [`mcp_server.py`](mcp_server.py) | Exposes Python functions as MCP tools. |
| Main pipeline | [`orchestrator/pipeline.py`](orchestrator/pipeline.py) | Builds context, routes requests, and coordinates the judge. |
| Result contracts | [`orchestrator/results.py`](orchestrator/results.py) | Defines typed component statuses and sanitized diagnostics. |
| Context loader | [`orchestrator/context.py`](orchestrator/context.py) | Resolves repositories and loads policy, Git state, and explicit files. |
| Task router | [`orchestrator/router.py`](orchestrator/router.py) | Classifies prompts as coding, operations, search, or general. |
| Model adapters | [`orchestrator/specialists.py`](orchestrator/specialists.py) | Assembles specialist messages without calling providers directly. |
| Model gateway | [`orchestrator/model_gateway.py`](orchestrator/model_gateway.py) | Owns every LiteLLM and HTTP model-provider call. |
| Egress guard | [`orchestrator/egress_guard.py`](orchestrator/egress_guard.py) | Enforces repository classification and scans complete payloads. |
| Scanner adapter | [`orchestrator/secret_scanner.py`](orchestrator/secret_scanner.py) | Runs local Gitleaks over stdin and exposes redacted metadata only. |
| Judge | [`orchestrator/judge.py`](orchestrator/judge.py) | Critiques a draft and optionally requests one revision. |
| RAG subsystem | [`orchestrator/rag.py`](orchestrator/rag.py) | Safety-scans, indexes, retrieves, and reranks repository text. |
| Safety rules | [`orchestrator/security.py`](orchestrator/security.py) | Rejects sensitive paths/content and defines index exclusions. |
| Executor workflow | [`orchestrator/workflow.py`](orchestrator/workflow.py) | Builds guarded prompts and launch commands for coding agents. |
| CLI | [`cli.py`](cli.py) | Provides `ask`, `plan`, `work`, `audit-index`, and `index` commands. |
| Claude MCP registration | [`claude-mcp.json`](claude-mcp.json) | Tells Claude Code how to start this MCP server. |

## The MCP Boundary

### How the server starts

`mcp_server.py` creates a `FastMCP` server named `orchestrator`. When run as the
main program, it starts using the `stdio` transport:

```python
mcp = FastMCP("orchestrator", instructions=_INSTRUCTIONS)

if __name__ == "__main__":
    mcp.run(transport="stdio")
```

Normally, the user does not start this process manually. The MCP client starts
the configured Python command as a child process and communicates with it over
standard input and output. FastMCP handles protocol negotiation, tool discovery,
argument parsing, and result framing.

The repository contains a Claude Code MCP configuration. Codex and VS Code use
equivalent client-side registrations described in [`SETUP.md`](SETUP.md).

### Exposed tools

The server exposes seven tools, including the five operational tools below and
the structured-plan validation tools described under approval enforcement.

#### `plan_task`

Inputs:

- `prompt`: the requested task.
- `repo_root`: the repository whose policy and state should be loaded.
- `context_path`: one optional safe file.
- `context_paths`: an optional list of safe files.

The tool calls `pipeline.plan()` and returns a structured proposal. It is marked
read-only in its MCP annotations.

#### `ask_orchestrator`

Inputs are the same as `plan_task`, plus:

- `use_judge`: whether to run critique and possible revision; defaults to `true`.

The tool calls `pipeline.run()` and returns only the pipeline's final answer. It
does not return the initial draft, classification, or other internal metadata.
This text-returning behavior is preserved for existing MCP clients.

#### `ask_orchestrator_structured`

Inputs are identical to `ask_orchestrator`. It calls the same pipeline and
returns the final answer together with sanitized overall status, per-component
status, warnings, retry-attempt counts, and a safe error code when applicable.
The diagnostic envelope contains no prompts, source chunks, credentials,
provider bodies, scanner output, or unrestricted exception text.

#### `audit_index`

This runs the index scanner without model calls and without writing ChromaDB. It
reports how many files and chunks would be indexed and why other candidates were
skipped.

#### `index_codebase`

This scans a source directory, sends accepted chunks to the configured REALMS
embedding service, and writes the resulting vectors to local ChromaDB storage.
Because a rebuild can delete and replace an existing vector collection, the MCP
tool is correctly annotated as mutating and destructive.

It changes the vector index, not the source repository. The project instructions
require an explicit request before this tool is called.

### Approval enforcement and MCP

The compatibility `plan_task` and `ask_orchestrator` tools remain independent
advisory calls. The Phase 1 interface adds `plan_task_structured` and
`validate_plan_approval`, while explicit approval creation and write-capable
execution remain CLI or client responsibilities.

The enforced CLI lifecycle adds a technical pre-execution boundary through:

1. A versioned, content-addressed plan record.
2. A separate, single-use human approval record.
3. Base-commit, working-tree, and policy-fingerprint validation.
4. A separate write-capable executor process.
5. Post-execution Git-visible changed-path validation.

There is still no implicit conversational state shared between MCP calls. The
structured records carry the state needed for deterministic validation.

## Context Assembly

Both planning and full-answer requests call `_build_context()` in
`orchestrator/pipeline.py`. It constructs one string from a trust-boundary notice
and up to five sources, in this order:

1. A notice that ordinary repository and RAG content is untrusted evidence.
2. Effective `AGENTS.md` guidance.
3. Sanitized caller constraints, when supplied.
4. Read-only Git state.
5. Explicitly requested safe files.
6. Repository-scoped RAG context.

The sections are separated by Markdown dividers before being passed to a model.

### Repository-root resolution

If `repo_root` is provided, it is expanded and resolved to an absolute path. It
must identify a directory. If it is omitted and explicit context files are
provided, the loader searches upward from the first file for the nearest `.git`
entry.

The CLI's `plan` and `work` commands apply a stricter check: their target must be
inside a Git repository.

### Effective agent guidance

The policy loader recognizes two filenames:

- `AGENTS.override.md`
- `AGENTS.md`

It walks from the effective workspace root toward the target directory. At each
level it chooses `AGENTS.override.md` when present; otherwise it chooses
`AGENTS.md`. Broader guidance is added first and more-specific guidance later,
allowing the more-specific instructions to take precedence.

If `RAG_SOURCE_DIRS` names an existing directory containing the repository, that
directory acts as the workspace root. Otherwise, the repository itself is the
policy root.

Policy text is capped at 32,768 bytes. When no explicit context file is supplied,
the policy target is the repository root, so policies in deeper directories are
not automatically loaded for a general repository-wide request.

### Read-only Git state

When the root contains `.git`, the loader captures:

```text
git rev-parse HEAD
git status --short --branch
git submodule status
git diff --check
```

Each command has a 15-second timeout. Failures are recorded as context rather
than aborting the request. Output is capped before it is added to the prompt.

This lets the planner see the base commit, working-tree state, submodule state,
and whitespace errors without allowing it to modify the repository.

### Explicit files

A caller may supply one or more repository files as extra context. Before a file
is read into the model prompt, the loader verifies that:

- It exists and is a regular file.
- It is within `repo_root`, when a root was provided.
- Its path is not classified as sensitive.
- Its content does not match a high-confidence secret pattern.

Each file contributes at most 16,000 characters, and all explicit files together
are capped at 64,000 bytes. A rejected explicit file raises an error rather than
silently omitting it.

### RAG context

After deterministic context is assembled, the pipeline asks the RAG subsystem
for semantically related chunks. When `repo_root` is supplied, the vector query
is filtered to chunks indexed with that exact repository root.

RAG retrieval is optional. The result contract distinguishes an absent index,
an empty index or valid zero-match query, stale-only chunks, dependency failure,
and an internal retrieval failure. When the primary specialist can still answer,
the pipeline continues without retrieved code and marks the overall response as
degraded where appropriate. Egress-policy and scanner failures are not
swallowed; they block the request. Legacy chunks without the current
egress-policy version are rejected before reranking and reported as stale.

## Model-egress boundary

Every Python provider call flows through `orchestrator/model_gateway.py`.
`orchestrator/egress_guard.py` first checks the active repository classification
and then invokes local Gitleaks on a deterministic serialization of the complete
model-bound payload. Authentication values are used only as provider credentials
and are excluded from model-content scanning and model messages.

Repositories default to `local-only`. They must contain an explicit
`.orchestrator-policy.toml` classification of `remote-approved` before content
can reach REALMS or another configured remote service. `deny-model` blocks local
and remote models. Invalid policy blocks use rather than guessing.

Tasks, caller constraints, effective guidance, and explicit context also receive
early source-aware scans. Index chunks are scanned before embedding. Retrieved
documents and their query are scanned before reranking. The final completion
scan covers the assembled system/user messages and any nested model-bound
options, closing call-site bypasses.

The initial prompt passed by the CLI to Codex or Claude is guarded immediately
before subprocess launch. Subsequent file reads or provider calls performed by
external agents and editor extensions are outside this Python boundary. See
[`SECURITY.md`](SECURITY.md) for classifications, setup, limitations, and the
sanitized-worktree requirement for stronger isolation.

## The Planning Flow

`plan_task` follows this path:

```text
MCP request
  -> mcp_server.plan_task()
  -> pipeline.plan()
  -> _build_context()
  -> fixed structured-planning instruction
  -> specialists.reason()
  -> REALMS gpt-oss-120b
  -> plan text returned through MCP
```

The fixed planning instruction asks the model to produce:

- Scope and base-state assumptions.
- Proposed file-by-file changes.
- Allowed and prohibited paths.
- Explicitly out-of-scope work.
- Checks the agent may run.
- Checks reserved for an authorized human.
- Human gates such as deployments, migrations, and secret access.
- Risks and assumptions.
- Required final handoff details.

Planning always uses the reasoning model. It does not use task classification,
specialist routing, or the judge pass. The returned plan is a proposal only; it
does not authorize implementation.

## The Full Answer Flow

`ask_orchestrator` follows this path:

```text
MCP request
  -> mcp_server.ask_orchestrator() or ask_orchestrator_structured()
  -> pipeline.run()
       1. classify_result(prompt)
       2. build context and retrieve_context_result()
       3. call the selected specialist result function
       4. produce a draft or an explicit failure
       5. critique_and_revise_result()
       6. aggregate component status and sanitized diagnostics
  -> plain final text or structured result returned through MCP
```

The complete internal result contains:

```python
{
    "status": "success | degraded_success | ...",
    "task_type": "coding | ops | search | general",
    "context_used": True | False,
    "retrieval_used": True | False,
    "repo_root": "/resolved/repository/or/None",
    "draft": "initial specialist answer",
    "final": "original or judge-revised answer",
    "warnings": [{"component": "...", "code": "...", "message": "..."}],
    "error": None | {"component": "...", "code": "...", "message": "..."},
    "components": [{"component": "...", "status": "...", "attempts": 1}],
}
```

The compatibility MCP wrapper exposes only `final`. The structured MCP tool
exposes the complete sanitized result, and the direct CLI prints overall status,
safe warnings, task type, actual retrieval use, and draft/revision differences.
Answer fields remain the model's verbatim output; only diagnostics are
restricted to safe codes and fixed messages.

## Classification and Specialist Routing

### Local classification

`orchestrator/router.py` sends the prompt to a local Ollama model, defaulting to
`qwen2.5:1.5b` at `http://localhost:11434`. It asks for exactly one category:

| Category | Intended work |
| --- | --- |
| `coding` | Writing, debugging, refactoring, or reviewing code. |
| `ops` | Infrastructure, Ansible, Terraform, CI/CD, or server administration. |
| `search` | Finding information in code or documentation. |
| `general` | Requests outside the other categories. |

Classification uses temperature zero and a five-token output limit. The HTTP
call has a ten-second timeout.

If Ollama is unavailable, the response is invalid, or another error occurs, a
keyword classifier is used. It tests operations keywords first, coding keywords
second, and search keywords third; otherwise it returns `general`. This ordering
means a prompt containing words from multiple groups may be routed according to
the earlier group rather than its main intent.

### Model assignments

`orchestrator/specialists.py` maps categories to these models:

| Use | Model and location |
| --- | --- |
| Coding | `Qwen3-Coder-Next` through REALMS |
| Operations | `gemma-4-31B-it` through REALMS |
| Search/summarization | `gemma-4-31B-it` through REALMS |
| General reasoning | `gpt-oss-120b` through REALMS |
| Planning | `gpt-oss-120b` through REALMS |
| Critique and revision | `gpt-oss-120b` through REALMS |
| Query and code embeddings | `Qwen3-Embedding-8B` through REALMS |
| RAG reranking | `Qwen3-Reranker-8B` through REALMS |
| Local classification | `qwen2.5:1.5b` through Ollama |
| Coding fallback | `qwen2.5-coder:7b` through Ollama |

REALMS requests use LiteLLM with an OpenAI-compatible endpoint. Configuration is
read from environment variables after loading the repository `.env` without
overriding variables already present in the shell.

The coding specialist attempts the local coding model only when the remote
provider is transiently unavailable or remote transmission is explicitly denied
while local model use remains allowed. Authentication, invalid request,
malformed configuration, scanner, and policy failures do not trigger that
fallback. Operations, search, planning, reasoning, and judge calls have no local
generation fallback. Each path returns or propagates a typed, sanitized outcome
rather than exposing a raw provider exception.

## The Judge Pass

The judge in `orchestrator/judge.py` is one critique stage followed by at most one
revision stage.

1. It receives the original request and the specialist draft.
2. It also receives the effective policy and repository context.
3. It asks `gpt-oss-120b` to identify factual errors, missing edge cases,
   security issues, and unnecessary complexity.
4. If the critique begins with `LGTM`, the draft becomes the final answer.
5. Otherwise, a second `gpt-oss-120b` call receives the request, draft, critique,
   and context and produces a corrected answer.

The environment variable `JUDGE_ENABLED` controls the default for direct
pipeline calls. The CLI's `--no-judge` option disables it per request, and the
MCP `use_judge` parameter makes the choice explicit.

The judge improves the odds of a useful response but cannot guarantee accuracy.
It is another model call and can miss or introduce errors. The executor must
still verify model claims against the source before implementing anything.
If critique or revision is unavailable, the pipeline retains the usable draft,
marks the request as degraded, and reports a sanitized judge warning. A
model-egress security block remains terminal and cannot be converted into a
degraded success.

## RAG Indexing and Retrieval

RAG—retrieval-augmented generation—lets the pipeline include relevant repository
snippets without placing the entire workspace into every prompt.

### Safety scan

`scan_directory()` recursively examines a source directory but prunes generated
or dependency directories such as:

- `.git`, `.venv`, `venv`, and `node_modules`
- Python, Ruff, mypy, tox, and other caches
- `.terraform` and `.terragrunt-cache`
- `build` and `dist`

Only a configured set of source and text extensions is considered. The scanner
also applies ordered patterns from `.orchestratorignore`, supports negation, and
uses `git check-ignore` separately within each detected nested Git repository.

Before reading a candidate into the index, it rejects:

- Symlinks.
- Files over 2,000,000 bytes.
- Binary-looking files.
- Git-ignored or orchestrator-ignored files.
- Sensitive filenames, directories, or suffixes.
- Text containing a recognized high-confidence secret.
- Empty files.

The scan report records counts for each skip reason.

### Chunking and storage

Accepted files are split into 1,500-character chunks. Each chunk receives a
SHA-256 identifier derived from its absolute path and character offset. Metadata
records:

- Source file path.
- Overall index source root.
- Nearest Git repository root.
- Character offset within the file.

Chunks are embedded in configurable batches, defaulting to 32, and stored in the
ChromaDB collection named `codebase`. The default persistent location is
`~/.orchestrator/chroma`, configurable with `RAG_INDEX_PATH`.

A rebuild deletes the existing collection before storing sanitized chunks. A
resume leaves existing IDs in place and only uploads missing IDs. Because IDs are
based on path and offset rather than content, resume should only be used for an
interrupted scan whose source has not changed.

### Query and reranking

For a prompt:

1. The query is embedded through REALMS.
2. Chroma returns up to eight nearest chunks.
3. An exact `repo_root` metadata filter is applied when provided.
4. REALMS reranks the candidates.
5. Up to three chunks are added to model context with source path and offset.

If reranking has a non-security failure, the original candidate order is used
and the caller receives a `degraded_success` warning. A missing index,
unavailable embedding/Chroma dependency, empty collection, no matches, and a
stale policy-version index are distinct outcomes. The main request may continue
without retrieved context, but its overall status records the degradation.
Model-egress security blocks remain terminal rather than becoming empty context.

## Secret and Context Safety

`orchestrator/security.py` defines path and content filters used by explicit
context loading, task validation, and RAG indexing.

Sensitive paths include common environment, credential, password, secret,
vault, private-key, certificate/key-store, Terraform state, and Terraform plan
names. Sensitive directories include `credentials`, `private_keys`, and
`secrets`.

High-confidence content detection recognizes:

- Ansible Vault ciphertext headers.
- PEM private-key headers.
- Common AWS access-key identifiers.
- GitHub token formats.
- Slack token formats.

These filters reduce accidental disclosure but are not a complete data-loss
prevention system. Unknown token formats, application-specific credentials, or
sensitive business data may not match them. Repository guidance and human review
remain authoritative.

## CLI Paths Through the System

The Typer CLI in `cli.py` exposes planning, approval, execution, advisory, and
RAG maintenance commands.

### `orchestrate plan`

Resolves a Git root and calls the pipeline directly. Without `--allow`, it keeps
the Markdown-only compatibility behavior. With one or more `--allow` values, it
creates a versioned plan record outside the target repository.

### `orchestrate approve` and `orchestrate execute`

`approve` validates current repository and policy state before creating a
single-use approval record. `execute` validates the exact plan and approval,
rechecks drift, consumes approval, launches a separate write-capable executor,
and validates Git-visible changed paths after the process exits.

### `orchestrate ask`

Calls `pipeline.run()` directly. It can include one safe file, explicitly set a
repository root, disable the judge, or use compatibility plan-first mode. It
prints classification, overall status, sanitized warnings, and whether retrieved
RAG context was actually used. The separate internal `context_used` field still
reports whether any deterministic or retrieved context was assembled.

### `orchestrate work`

This is now a compatibility read-only planning entry point. It resolves the
target Git root, validates task text, and then:

- Launches Codex by default.
- Launches Claude Code with `--executor claude`.
- Prints the launch details without starting an agent with `--print-only`.

The launched agent receives instructions to inspect the repository, call
`plan_task`, show the proposal, and stop. It cannot be elevated in place.

Codex uses a read-only sandbox and Claude Code uses plan permission mode.

### `orchestrate audit-index`

Calls the scanner and prints what would be indexed. It performs no embedding
requests and does not write ChromaDB.

### `orchestrate index`

Runs the safety scan, embeds accepted chunks, writes the ChromaDB index, and
prints progress and a summary. Rebuild is the default; resume is intended only
for an unchanged interrupted source scan.

## A Complete Guarded Work Session

The intended end-to-end sequence is:

1. The human requests a structured plan with an exact repository and explicit
   allowed paths.
2. The planner loads policy and repository state read-only, obtains architect
   advice, and writes the plan record outside the target repository.
3. The human reviews the proposal and structured scope.
4. `approve` verifies that repository and policy state are unchanged, then
   creates a single-use approval record.
5. `execute` revalidates the exact task, plan, repository, policy, and approval.
6. The approval is consumed before a separate write-capable executor starts.
7. The executor calls `ask_orchestrator` with the same task, repository root,
   and effective caller constraints.
8. The executor evaluates the advice, edits only approved paths, runs permitted
   checks, and reports its handoff.
9. The CLI rejects commits and validates final Git-visible changed paths against
   the plan allowlist.

The orchestrator is therefore the **architect and reviewer**, MCP is the
**tool-call bridge**, the coding agent is the **executor**, and the human remains
the **approval authority**.

## Technical Superstructure

The system is easiest to reason about as four planes. These are conceptual
boundaries rather than Python packages, but they clarify which component owns
each decision.

```text
Integration plane
  cli.py | FastMCP | Codex/Claude launchers
       |
       v
Orchestration plane
  pipeline.py | router.py | judge.py
       |
       +-------------------+
       v                   v
Context plane          Model plane
  context.py             specialists.py
  rag.py                 REALMS
  security.py            Ollama
  Git + ChromaDB
```

### Integration plane

The integration plane converts an external action into a Python function call.
The CLI calls pipeline functions in-process. MCP clients call the same pipeline
through a FastMCP subprocess. The `work` command is different again: it launches
a coding agent and gives that agent a prompt instructing it to call MCP later.

This means there are two orchestration meanings in the repository:

1. **Model orchestration** inside `pipeline.py`: context, routing, generation,
   and review.
2. **Work orchestration** inside `workflow.py`: plan, human approval, advice,
   external editing, validation, and handoff.

Keeping those meanings separate prevents a common misunderstanding: the model
pipeline never becomes the code executor merely because it was reached through
the `work` command.

### Orchestration plane

The orchestration plane owns sequencing but very little domain logic. The
pipeline is a synchronous coordinator. It calls each stage in order, carries
plain strings between stages, and returns a small dictionary.

There is no internal task queue, worker pool, event stream, durable run record,
or workflow state machine. A request exists for the lifetime of a single Python
call. The only durable orchestration-related state is the independently managed
ChromaDB vector index.

### Context plane

The context plane decides what evidence a model receives. Deterministic inputs
such as policy, Git state, and explicit files are assembled first. Probabilistic
RAG matches are appended afterward. The result is flattened into one Markdown
string rather than retained as typed evidence objects.

This plane is also the main trust boundary. It reads local repository material
and may transmit accepted content to REALMS. `security.py` protects that boundary
with exclusions and high-confidence secret detection, while repository scoping
reduces cross-project retrieval.

### Model plane

The model plane contains thin adapters, not autonomous agents. A specialist
function constructs a list of chat messages and makes one completion call. The
judge makes up to two additional reasoning calls. None of these adapters can
open files, execute commands, call MCP tools, or edit the workspace.

The external coding agent is the only agentic executor in the normal workflow.
It has its own tool loop, sandbox, and conversation state outside this Python
pipeline.

## Static Dependency Direction

The import structure is mostly one-directional:

```text
mcp_server.py
  +-- pipeline.py
  |     +-- context.py ----> security.py
  |     +-- router.py
  |     +-- rag.py --------> security.py
  |     +-- specialists.py
  |     +-- judge.py ------> specialists.py
  +-- rag.py

cli.py
  +-- workflow.py ---------> context.py, security.py
  +-- pipeline.py          (lazy import for ask/plan)
  +-- rag.py               (lazy import for index commands)
```

The lazy CLI imports keep lightweight commands from loading model and vector
dependencies unnecessarily. The package-level `orchestrator/__init__.py` uses
`__getattr__` for the same reason.

The main coupling point is the flattened context string. `pipeline.py`, every
specialist, and the judge agree informally on its meaning, but no schema enforces
which sections are present or how a model should cite them.

## Data and Control Contracts

### MCP contract

FastMCP derives each tool schema from the Python signature and docstring. The
legacy `ask_orchestrator` contract still consists of primitive arguments and a
returned final-answer string. The additive `ask_orchestrator_structured` tool
uses the same arguments and returns the pipeline's JSON-compatible diagnostic
envelope. There is no repository-defined protocol-buffer, REST, or domain-event
schema.

This split preserves existing clients while allowing new clients to distinguish
warnings, component status, retries, retrieval use, and sanitized failures from
ordinary answer prose.

### Context contract

Context is a single string with labelled Markdown sections:

```text
### Effective agent guidance: <path>
...

---

### Read-only repository state
...

---

### Explicit context: <path>
...

---

### Retrieved context: <path> (offset <n>)
...
```

The specialist receives this as a system-role message labelled `Relevant
context`. The judge receives it after its critique or revision instruction,
labelled `Effective policy and context`.

The ordering communicates priority to the model but does not technically isolate
policy from untrusted repository content. A source file containing prompt-like
instructions remains text within the same overall context.

### Result contract

Internally, `pipeline.run()` separates draft and final output and aggregates
typed `ComponentResult` values. Status values are `success`,
`degraded_success`, `unavailable_dependency`, `invalid_input`,
`invalid_configuration`, `security_block`, and `internal_failure`. Component
summaries contain only component, status, code, fixed message, attempt count,
and sanitized warning metadata. The legacy MCP adapter collapses this result to
the final string; the structured adapter preserves it. `pipeline.plan()` still
returns unvalidated Markdown. Required plan headings are requested through
prompting rather than validated after generation.

Phase 1 wraps an architect proposal in a typed, versioned record containing the
exact task, repository snapshot, policy identity, allowed paths, prohibited
operations, and required checks. The proposal remains model-generated prose,
but approval and execution validation use the structured envelope.

## Configuration and Initialization

Configuration is environment-driven:

| Variable | Consumer | Meaning |
| --- | --- | --- |
| `REALMS_BASE_URL` | specialists and RAG | OpenAI-compatible completion, embedding, and reranking endpoint. |
| `REALMS_API_KEY` | specialists and RAG | Credential for REALMS calls. |
| `OFFLINE_MODE` | specialists | Prevents REALMS completion calls. |
| `OLLAMA_BASE_URL` | router and specialists | Local Ollama service URL. |
| `OLLAMA_ROUTER_MODEL` | router | Local classification model. |
| `OLLAMA_CODING_MODEL` | specialists | Local coding fallback model. |
| `MODEL_REMOTE_MAX_ATTEMPTS` | model gateway | Bounded remote attempts for transient failures; default 3, valid range 1-5. |
| `MODEL_RETRY_BASE_SECONDS` | model gateway | Exponential retry base delay; default 0.25 seconds, valid range 0-5. |
| `JUDGE_ENABLED` | judge | Default judge behavior for calls without an explicit choice. |
| `RAG_INDEX_PATH` | RAG | Persistent ChromaDB location. |
| `RAG_EMBED_BATCH_SIZE` | RAG | Number of chunks embedded per batch. |
| `RAG_SOURCE_DIRS` | context, CLI, and MCP | Default index source and possible policy workspace root. |
| `PLAN_FIRST` | CLI | Enables compatibility plan-and-confirm behavior for `ask`. |
| `ORCHESTRATOR_STATE_DIR` | approval workflow | User-local structured plan and approval record storage. |

The `.env` file is loaded without overriding shell variables, so an exported
value wins. Most settings become module-level constants at import time. Changing
those environment variables after import will not reconfigure the router,
specialists, or RAG module. `JUDGE_ENABLED` is a notable exception because it is
read when `critique_and_revise()` is called.

There is no centralized configuration object or startup validation pass. A
missing REALMS credential is normally discovered when a completion is attempted.

## Synchronous Execution and Failure Propagation

All repository-defined stages are synchronous. One MCP tool invocation blocks
while it performs context collection and remote model calls. The model gateway
defines bounded exponential retries for narrowly classified transient remote
failures. The code defines no concurrency limit, rate limiter, circuit breaker,
request cache, or cancellation propagation; any concurrency behavior above it
belongs to FastMCP or the MCP client.

Timeouts are unevenly applied:

| Operation | Repository-defined timeout |
| --- | --- |
| Local Ollama classification | 10 seconds |
| Each read-only Git command | 15 seconds |
| RAG reranking HTTP request | 60 seconds |
| MCP `plan_task` / `ask_orchestrator` annotation | 300 seconds |
| MCP `audit_index` annotation | 120 seconds |
| MCP `index_codebase` annotation | 600 seconds |
| LiteLLM completions and embeddings | No explicit timeout in this code |

Failure behavior differs by stage:

- Router failure degrades visibly to keyword classification.
- RAG reranking failure preserves vector-search order with a warning.
- Broader RAG failures are classified separately from valid zero-match results.
- Git inspection failure is included as text in the context.
- Rejected explicit context raises immediately.
- Missing or malformed REALMS/retry configuration is terminal and not retried.
- Transient remote provider failures are retried within configured bounds.
- Coding generation may fall back locally only under its explicit safe policy.
- Judge unavailability retains a usable draft and marks the result degraded.
- Scanner or model-egress policy blocks are terminal and never retried or
  converted into an ordinary fallback.

Public failures use fixed codes and messages. Raw exception text, provider
bodies, scanner output, prompts, credentials, and source chunks are excluded
from the diagnostic contract.

## Trust Boundaries

```text
Trusted control input
  Human approval + effective AGENTS.md
             |
             v
Partially trusted orchestration
  Python code + Git metadata + MCP arguments
             |
             v
Potentially untrusted content
  repository files + indexed chunks + user-supplied prompt text
             |
             v
External processing boundary
  REALMS completion / embedding / reranking APIs
             |
             v
Untrusted advisory output
  specialist and judge prose
             |
             v
Enforcement point
  coding agent policy, sandbox, human approval, and source verification
```

The most important design principle is that the enforcement point remains
outside the model pipeline. A model response cannot grant itself permission to
write, deploy, access secrets, or expand scope.

## Engineering Improvement Opportunities

This list records both delivered increments and remaining work so it is not
mistaken for an implementation-status list.

### 1. Return structured results — Phase 2B partially delivered

Typed component and pipeline outcomes, safe warnings, failure categories, and an
additive structured MCP result are implemented. A typed context/evidence
manifest and deterministic validation of model-generated plan prose remain
future work.

### 2. Bind approval to a specific plan and repository state — Phase 1 delivered

The CLI lifecycle now binds task, normalized repository root, Git state, policy
fingerprint, allowed paths, and structured plan content to a single-use approval
record. Compatibility MCP calls remain advisory and independent by design. The
coding agent remains responsible for edits and verification.

### 3. Make evidence visible and verifiable

Return a context manifest listing every policy file, explicit file, Git command,
and RAG chunk used. Require model claims about repository structure to cite that
manifest. The executor could then reject references to paths that were never
observed.

### 4. Separate policy from untrusted content

Represent policy, user intent, repository evidence, and retrieved snippets as
distinct typed message sections with explicit trust labels. Add prompt-injection
instructions for repository content, and never allow retrieved source text to
override `AGENTS.md` or human constraints.

### 5. Strengthen request-side secret controls — Phase 2A delivered

All model-capable paths cross the centralized fail-closed Gitleaks and egress
policy boundary, including MCP-originated requests, provider payloads, RAG
embedding/reranking, and the initial external-agent prompt. Organization-specific
detectors and a physically isolated sanitized worktree remain optional stronger
controls.

### 6. Make RAG updates content-addressed and atomic

Include a content hash in each chunk ID. Build a new versioned collection and
atomically switch an alias after all embeddings succeed. That would make resume
safe after content changes and prevent a failed rebuild from leaving a partially
populated live collection.

### 7. Surface degraded behavior — Phase 2B delivered

RAG outcomes now distinguish index absence, dependency failure, stale content,
zero matches, and reranker fallback. CLI and structured MCP responses expose
sanitized warnings and actual retrieval use separately from aggregate context.

### 8. Centralize validated configuration

Create an immutable settings object at startup, validate required endpoints and
credentials for enabled features, and inject settings into router, specialist,
and RAG services. This would reduce import-time global state and make tests and
runtime reconfiguration more predictable.

### 9. Add explicit resilience controls — Phase 2B partially delivered

Bounded transient-only remote retries and safe attempt/fallback diagnostics are
implemented. Consistent per-operation timeouts, rate limits, circuit breakers,
and cancellation propagation remain future operational work.

### 10. Improve routing confidence

Return a classification confidence and rationale code, test ambiguous mixed
prompts, and allow the pipeline to choose a safe general model when confidence
is low. Routing policy could be data-driven rather than embedded in module-level
conditionals.

### 11. Use evidence-aware review

The current judge reviews prose with another model. Add deterministic checks
before or after it: validate mentioned repository paths, commands, model names,
and proposed write scope against the context manifest and policy. A second model
alone cannot reliably detect hallucinated architecture.

### 12. Add observability without leaking context — diagnostic foundation delivered

The result contract now supplies safe component, failure, fallback, and attempt
metadata without sensitive payloads. Request IDs, latency events, aggregate
metrics, and an external telemetry sink remain future work; any such sink must
continue excluding prompts, file contents, credentials, and raw model context.

### 13. Expand integration and contract tests — Phase 2B coverage added

Focused tests now cover transient retry bounds, non-retryable security and
configuration paths, classifier fallback, retrieval state distinctions,
reranker and pipeline degradation, diagnostic redaction, and legacy/structured
MCP compatibility. Full in-process FastMCP schema, cancellation, and
deployment-level integration tests remain future work.

## Failure Behavior and Important Limitations

### Model output can be wrong

Specialist and judge results are natural-language model output. Neither the MCP
adapter nor the judge proves that cited files, packages, commands, or behaviors
exist. Source inspection by the executor is mandatory.

### Scope enforcement is post-execution

The CLI enforces approval before launching a write-capable executor and rejects
stale or consumed approvals. Allowed-path validation occurs after the executor
exits and covers Git-visible changes. It is not an operating-system path sandbox
and does not automatically roll back violations, because rollback could destroy
unrelated work.

### Context may be incomplete

RAG can return no usable chunks with a visible status, policy text is
size-limited, explicit files are truncated, and deeper directory guidance is
loaded only when the target points there. A model answer may therefore be based
on partial context even when diagnostics accurately describe retrieval state.

### Secret detection is intentionally narrow

The filters target recognizable, high-confidence patterns to avoid excessive
false positives. They cannot guarantee that every secret or sensitive datum is
excluded.

### Routing is approximate

The local classifier and keyword fallback can choose the wrong specialist.
Keyword ordering can dominate mixed prompts, and there is no second routing
validation stage.

### Fallback coverage is intentionally narrow

Only coding generation has a local Ollama fallback, and only for a classified
transient remote failure or a remote-transmission denial that still permits
local model use. Planning, operations, search, general reasoning, embeddings,
reranking, and judging depend on REALMS, aside from their documented degraded
result behavior. Authentication, invalid request/configuration, scanner, and
policy failures never activate a bypassing fallback.

### RAG resume assumes unchanged content

Chunk IDs use path and offset, not a content hash. Resuming after source changes
can leave existing vectors for changed chunks, which is why a safe full rebuild
is the normal operation.

## What the Tests Cover

The repository tests verify the most important boundaries:

- `test_context.py` checks guidance precedence and rejection of sensitive or
  out-of-repository explicit files.
- `test_rag_safety.py` checks sensitive-path detection, ignore patterns, safe
  indexing, repository filtering, and rebuild/resume behavior.
- `test_judge_configuration.py` checks environment and per-call judge controls,
  including MCP argument forwarding.
- `test_cli_plan.py` verifies that plan-only mode does not enter execution or
  approval paths and requires a Git repository.
- `test_workflow.py` verifies repository resolution, guarded prompt contents,
  Codex sandbox arguments, and rejection of secret material in task text.
- `test_phase2b_reliability.py` checks bounded transient retries, terminal
  security/configuration paths, visible component degradation, diagnostic
  redaction, and legacy/structured MCP compatibility.

These tests validate wiring and safety behavior with mocks. They do not make
model correctness deterministic, and they do not turn the advisory orchestrator
into an enforcement or execution service.
