"""
cli.py — command-line interface for the orchestrator.
Usage: uv run python cli.py "your prompt here"
"""

import os
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=False)  # shell env vars take priority

from orchestrator.pipeline import run, plan
from orchestrator.rag import index_directory, scan_directory

app = typer.Typer(help="REALMS + local model orchestrator")
console = Console()


@app.command()
def ask(
    prompt: str = typer.Argument(..., help="Your question or task"),
    file: Path = typer.Option(None, "--file", "-f", help="Path to a file to include as context"),
    no_judge: bool = typer.Option(False, "--no-judge", help="Skip the critique/revision pass"),
    plan_only: bool = typer.Option(False, "--plan", help="Show a plan and ask for approval before executing"),
    repo_root: Path = typer.Option(
        None,
        "--repo-root",
        help="Target Git repository; loads effective AGENTS.md and scopes RAG",
    ),
):
    """Send a prompt through the full router → specialist → judge pipeline."""
    ctx_path = str(file) if file else None

    # Plan-first mode: propose changes, gate on approval
    if plan_only or os.getenv("PLAN_FIRST", "false").lower() == "true":
        console.print("[dim]Generating plan...[/dim]")
        proposal = plan(
            prompt,
            context_path=ctx_path,
            repo_root=str(repo_root) if repo_root else None,
        )
        console.print(Panel(Markdown(proposal), title="[bold yellow]Plan (no changes made)[/bold yellow]", border_style="yellow"))
        approved = typer.confirm("\nProceed with implementation?", default=False)
        if not approved:
            console.print("[dim]Aborted — no changes made.[/dim]")
            raise typer.Exit()
        console.print("[dim]Approved. Running implementation...[/dim]")

    console.print(f"[dim]Classifying prompt...[/dim]")
    result = run(
        prompt,
        context_path=ctx_path,
        judge_enabled=False if no_judge else None,
        repo_root=str(repo_root) if repo_root else None,
    )

    console.print(f"[bold cyan]Task type:[/bold cyan] {result['task_type']}")
    console.print(f"[bold cyan]RAG context:[/bold cyan] {'yes' if result['context_used'] else 'no'}")

    if result["draft"] != result["final"]:
        console.print(Panel(Markdown(result["draft"]), title="Draft", border_style="yellow"))
        console.print(Panel(Markdown(result["final"]), title="Revised (judge pass)", border_style="green"))
    else:
        console.print(Panel(Markdown(result["final"]), title="Answer", border_style="green"))


@app.command()
def index(
    source: Path = typer.Argument(
        Path(os.getenv("RAG_SOURCE_DIRS", "/Users/jelambeadmin/Documents/access-sysops")),
        help="Directory to index for RAG",
    ),
    rebuild: bool = typer.Option(
        True,
        "--rebuild/--resume",
        help="Rebuild safely, or resume an interrupted scan whose source is unchanged",
    ),
):
    """Safety-scan and index a directory into the local ChromaDB vector store."""
    console.print(f"[dim]Scanning {source} before transmission...[/dim]")

    def show_progress(done: int, total: int) -> None:
        console.print(f"[dim]Embedded {done}/{total} chunks[/dim]")

    report = index_directory(
        str(source),
        rebuild=rebuild,
        progress=show_progress,
    )
    console.print(f"[green]{report.summary()}[/green]")


@app.command("audit-index")
def audit_index(
    source: Path = typer.Argument(
        Path(os.getenv("RAG_SOURCE_DIRS", "/Users/jelambeadmin/Documents/access-sysops")),
        help="Directory to safety-scan",
    ),
):
    """Report what would be indexed without making API calls or writing Chroma."""
    _, report = scan_directory(str(source))
    console.print(report.summary(audit=True))


if __name__ == "__main__":
    app()
