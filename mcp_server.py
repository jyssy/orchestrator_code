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
from orchestrator.pipeline import run, plan
from orchestrator.rag import index_directory

mcp = FastMCP("orchestrator")


@mcp.tool()
def ask_orchestrator(prompt: str, context_path: str = "") -> str:
    """
    Route a prompt through the full REALMS + local model pipeline.
    Optionally pass a file path to include as additional context.
    Returns the final answer after any judge revision.
    """
    result = run(prompt, context_path=context_path or None)
    return result["final"]


@mcp.tool()
def plan_task(prompt: str, context_path: str = "") -> str:
    """
    Generate a structured plan for a task without executing it.
    Returns: scope, proposed changes, what won't change, required checks,
    human gates, and risks. Present to user for approval before acting.
    """
    return plan(prompt, context_path=context_path or None)
    return result["final"]


@mcp.tool()
def index_codebase(source_dir: str = "") -> str:
    """
    Index a directory into the local RAG vector store.
    Defaults to the RAG_SOURCE_DIRS env variable if no path given.
    """
    target = source_dir or os.getenv("RAG_SOURCE_DIRS", "/Users/jelambeadmin/Documents/access-sysops")
    count = index_directory(target)
    return f"Indexed {count} chunks from {target}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
