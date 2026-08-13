"""
mcp_server.py — exposes the orchestrator pipeline as MCP tools
so VS Code Copilot (and other MCP clients) can call it.

Start with: uv run python mcp_server.py
Register in VS Code settings.json (see SETUP.md Phase 4).
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from fastmcp import FastMCP

from orchestrator.approval import validate_approval
from orchestrator.context import capture_repository_snapshot, reload_policy_identity
from orchestrator.models import ApprovalRecord, StructuredPlan
from orchestrator.pipeline import plan, plan_structured, run
from orchestrator.rag import index_directory, scan_directory

_INSTRUCTIONS = """This server is advisory: its tools generate plans and answers but
never edit files or run validation commands. For a requested change, call plan_task
first with the target repo_root and present the result for approval. After approval,
call ask_orchestrator with the same repo_root. Effective AGENTS.md guidance is
authoritative. The MCP client may edit files and run only checks that guidance permits;
it must report prohibited checks as pending human actions. Call index_codebase only
when the user explicitly asks to refresh the RAG index. For an enforceable lifecycle,
use plan_task_structured and validate_plan_approval with an external human approval
record; the CLI owns approval creation and write-capable executor launch."""

mcp = FastMCP("orchestrator", instructions=_INSTRUCTIONS)


@mcp.tool(
    timeout=300,
    annotations={"readOnlyHint": True, "openWorldHint": True},
)
def ask_orchestrator(
    prompt: str,
    context_path: str = "",
    context_paths: list[str] | None = None,
    repo_root: str = "",
    use_judge: bool = True,
    effective_constraints: str = "",
) -> str:
    """
    Route a prompt through the full REALMS + local model pipeline.
    Pass repo_root so effective AGENTS.md policy and repo-scoped RAG are loaded.
    Optionally pass one or more safe files as additional context.
    Set use_judge=false for a faster single-pass response.
    Returns the final answer after any requested judge revision.
    """
    result = run(
        prompt,
        context_path=context_path or None,
        context_paths=context_paths,
        repo_root=repo_root or None,
        judge_enabled=use_judge,
        effective_constraints=effective_constraints,
    )
    if not result["final"]:
        error = result.get("error") or {
            "message": "The orchestration request did not produce an answer."
        }
        raise RuntimeError(error["message"])
    return result["final"]


@mcp.tool(
    timeout=300,
    annotations={"readOnlyHint": True, "openWorldHint": True},
)
def ask_orchestrator_structured(
    prompt: str,
    context_path: str = "",
    context_paths: list[str] | None = None,
    repo_root: str = "",
    use_judge: bool = True,
    effective_constraints: str = "",
) -> dict:
    """Return the answer plus sanitized component statuses and warnings."""
    return run(
        prompt,
        context_path=context_path or None,
        context_paths=context_paths,
        repo_root=repo_root or None,
        judge_enabled=use_judge,
        effective_constraints=effective_constraints,
    )


@mcp.tool(
    timeout=300,
    annotations={"readOnlyHint": True, "openWorldHint": True},
)
def plan_task(
    prompt: str,
    context_path: str = "",
    context_paths: list[str] | None = None,
    repo_root: str = "",
    effective_constraints: str = "",
) -> str:
    """
    Generate a structured plan for a task without executing it.
    Returns: scope, proposed changes, what won't change, required checks,
    human gates, and risks. Pass repo_root to load effective AGENTS.md guidance
    and repository state. Present the result for approval before acting.
    """
    return plan(
        prompt,
        context_path=context_path or None,
        context_paths=context_paths,
        repo_root=repo_root or None,
        effective_constraints=effective_constraints,
    )


@mcp.tool(
    timeout=300,
    annotations={"readOnlyHint": True, "openWorldHint": True},
)
def plan_task_structured(
    prompt: str,
    repo_root: str,
    allowed_paths: list[str],
    effective_constraints: str = "",
    context_path: str = "",
    context_paths: list[str] | None = None,
) -> str:
    """Return a versioned plan record without writing it or editing the repository."""
    structured = plan_structured(
        prompt,
        repo_root=repo_root,
        allowed_paths=allowed_paths,
        effective_constraints=effective_constraints,
        context_path=context_path or None,
        context_paths=context_paths,
    )
    return structured.to_json()


@mcp.tool(
    timeout=120,
    annotations={"readOnlyHint": True, "openWorldHint": False},
)
def validate_plan_approval(plan_json: str, approval_json: str) -> str:
    """Validate an externally recorded human approval against current state."""
    structured = StructuredPlan.from_json(plan_json)
    approval = ApprovalRecord.from_json(approval_json)
    snapshot = capture_repository_snapshot(Path(structured.repository.repo_root))
    policy = reload_policy_identity(
        structured.policy.sources, structured.effective_constraints
    )
    validate_approval(
        structured,
        approval,
        current_base_commit=snapshot.base_commit,
        current_working_tree_fingerprint=snapshot.working_tree_fingerprint,
        current_policy_fingerprint=policy.fingerprint,
    )
    return f"Approval is valid for plan {structured.plan_id}."


@mcp.tool(
    timeout=600,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def index_codebase(source_dir: str = "", rebuild: bool = True) -> str:
    """
    Index a directory into the local RAG vector store.
    Defaults to the RAG_SOURCE_DIRS env variable if no path given. Files are
    safety-scanned before transmission. Rebuild defaults true so stale chunks
    cannot survive; use rebuild=false only to resume an unchanged interrupted scan.
    """
    target = source_dir or os.getenv("RAG_SOURCE_DIRS", "/Users/jelambeadmin/Documents/access-sysops")
    report = index_directory(target, rebuild=rebuild)
    return report.summary()


@mcp.tool(
    timeout=120,
    annotations={"readOnlyHint": True, "openWorldHint": False},
)
def audit_index(source_dir: str = "") -> str:
    """Safety-scan an index source without reading secrets or calling model APIs."""
    target = source_dir or os.getenv("RAG_SOURCE_DIRS", "/Users/jelambeadmin/Documents/access-sysops")
    _, report = scan_directory(target)
    return report.summary(audit=True)


if __name__ == "__main__":
    mcp.run(transport="stdio")
