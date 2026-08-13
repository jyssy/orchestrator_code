# Model-Egress and Secret-Scanning Boundary

Phase 2A places deterministic authorization and local secret scanning before
model transmission performed by this Python orchestrator.

## Required controls

Gitleaks is mandatory. Every actual local or remote model request fails closed
when Gitleaks is missing, times out, exits unexpectedly, or returns malformed
finding metadata. Findings exposed to callers contain only a rule identifier,
optional line number, and count. Payload values, scanner stdout, and scanner
stderr are never included in orchestrator errors.

The adapter sends content to Gitleaks over standard input and requests a fully
redacted JSON report in a private temporary directory. Existing high-confidence
path and content filters remain an earlier defense, not a scanner substitute.
Gitleaks runs from that temporary directory with a minimal environment so an
untrusted target repository cannot replace its rules through repository-local
configuration.

Provider calls are centralized in `orchestrator/model_gateway.py`. Routing,
specialist completion, planning, judging, revision, embedding, and reranking all
cross the same guard. The initial prompt used to launch Codex or Claude from the
CLI is also guarded immediately before launch and before approval consumption.

## Safe failure and retry behavior

Phase 2B adds typed component outcomes and sanitized public diagnostics. The
public status categories are success, degraded success, unavailable dependency,
invalid input, invalid configuration, security-policy block, and internal
failure. Diagnostic messages are fixed descriptions; they never include model
prompts, repository chunks, credentials, provider request or response bodies,
scanner output, or unrestricted exception text. Successful answer text is not
silently rewritten as part of diagnostic sanitization.

Only transient remote transport failures, rate limits, and selected server
errors receive bounded retry attempts. Scanner findings or scanner failure,
model-egress policy denial, authentication failure, invalid requests, and
malformed configuration are terminal and cannot trigger a retry or fallback
that bypasses the guard. Every provider attempt still crosses the centralized
model-egress boundary before transmission.

## Repository classifications

Each repository may define `.orchestrator-policy.toml`:

```toml
[model_egress]
classification = "local-only"
```

The supported values are:

| Classification | Effect |
| --- | --- |
| `deny-model` | Blocks local and remote model calls. |
| `local-only` | Allows scanned payloads only to local models. This is the default when the file is absent. |
| `remote-approved` | Allows scanned payloads to local models and configured remote services. |

An invalid, unreadable, oversized, or symlinked policy is ambiguous and blocks
model use. Remote indexing accepts files only from repositories explicitly set
to `remote-approved`. The policy file itself is excluded from RAG content. Its
presence and contents are bound into new structured-plan policy fingerprints,
so a classification change invalidates approval.

Copy `.orchestrator-policy.example.toml` into a target repository as
`.orchestrator-policy.toml` and choose the narrowest classification that works.
Setting `remote-approved` is authorization to attempt transmission, not a claim
that the repository contains no secrets; mandatory scanning still applies.

## Scanned content

The orchestrator performs source-aware scans of user tasks, caller constraints,
effective `AGENTS.md` guidance, and explicit context. It then scans the fully
assembled payload at the final provider boundary. RAG chunks are scanned before
embedding, and retrieved chunks are scanned with the query before reranking and
again as part of any assembled completion payload.

Ordinary repository and RAG content is labelled untrusted evidence in model
context. It cannot become policy merely by containing instructions. Effective
agent guidance and explicit caller constraints remain separately labelled
policy inputs.

## Existing RAG indexes

New chunks carry an egress-policy version. Retrieval rejects chunks without the
current version, so legacy content cannot be reranked or added to a completion.
After installing Gitleaks and adding repository opt-ins, an authorized human
should run a full `index --rebuild`; `--resume` cannot upgrade legacy chunk
metadata.

## Boundary limitations

This control covers content transmitted through the Python orchestrator and the
initial CLI-generated Codex or Claude prompt. It cannot intercept independent
file reads or provider calls made later by Codex, Claude Code, VS Code Copilot,
Continue, editor extensions, shell tools, or other software. A `.gitignore`,
`.claudeignore`, prompt instruction, or secret scanner is not an operating-system
read barrier.

For a literal guarantee that an external agent cannot receive source secrets,
give it a separately prepared sanitized worktree or enforce an OS/container
filesystem boundary that excludes prohibited material. Do not ask a model to
inspect vault, credential, private-key, environment, or Terraform-state files.

The scanner reduces accidental disclosure risk; it cannot prove that arbitrary
confidential business data is safe to transmit. Repository classification and
human judgment remain required.
