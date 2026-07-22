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

load_dotenv(Path(__file__).parent / ".env")

from orchestrator.pipeline import run
from orchestrator.rag import index_directory

app = typer.Typer(help="REALMS + local model orchestrator")
console = Console()


@app.command()
def ask(
    prompt: str = typer.Argument(..., help="Your question or task"),
    file: Path = typer.Option(None, "--file", "-f", help="Path to a file to include as context"),
    no_judge: bool = typer.Option(False, "--no-judge", help="Skip the critique/revision pass"),
):
    """Send a prompt through the full router → specialist → judge pipeline."""
    if no_judge:
        os.environ["JUDGE_ENABLED"] = "false"

    console.print(f"[dim]Classifying prompt...[/dim]")
    result = run(prompt, context_path=str(file) if file else None)

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
):
    """Index a directory into the local ChromaDB vector store."""
    console.print(f"[dim]Indexing {source} ...[/dim]")
    count = index_directory(str(source))
    console.print(f"[green]Indexed {count} chunks from {source}[/green]")


if __name__ == "__main__":
    app()
