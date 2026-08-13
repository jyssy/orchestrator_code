"""
cli.py — command-line interface for the orchestrator.
Usage: uv run python cli.py "your prompt here"
"""

import os
import shlex
import subprocess
from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

load_dotenv(Path(__file__).parent / ".env", override=False)  # shell env vars take priority

from orchestrator.approval import (
    consume_approval,
    create_approval,
    load_approval,
    load_plan,
    save_approval,
    save_plan,
    validate_approval,
    validate_approval_location,
    validate_plan_state,
)
from orchestrator.context import (
    capture_path_metadata,
    capture_repository_snapshot,
    reload_policy_identity,
)
from orchestrator.workflow import (
    DEFAULT_EFFECTIVE_CONSTRAINTS,
    Executor,
    build_claude_command,
    build_codex_command,
    build_codex_prompt,
    build_copilot_prompt,
    build_execution_command,
    build_execution_prompt,
    guard_external_agent_prompt,
    resolve_target_repo,
    validate_execution_result,
)

app = typer.Typer(help="REALMS + local model orchestrator")
console = Console()


def run(*args, **kwargs):
    """Load the model pipeline only when an ask command actually needs it."""
    from orchestrator.pipeline import run as pipeline_run

    return pipeline_run(*args, **kwargs)


def run_plan(*args, **kwargs):
    """Load the model pipeline only when a command needs a plan."""
    from orchestrator.pipeline import plan as pipeline_plan

    return pipeline_plan(*args, **kwargs)


def run_structured_plan(*args, **kwargs):
    """Load the model pipeline only when a structured plan is requested."""
    from orchestrator.pipeline import plan_structured

    return plan_structured(*args, **kwargs)


@app.command()
def work(
    task: str = typer.Argument(..., help="Coding task to plan and implement"),
    repo_root: Path = typer.Option(
        None,
        "--repo-root",
        "-r",
        help="Target repository or a directory inside it; defaults to the current directory",
    ),
    executor: Executor = typer.Option(
        Executor.CODEX,
        "--executor",
        "-e",
        help="Code executor to prepare",
        case_sensitive=False,
    ),
    print_only: bool = typer.Option(
        False,
        "--print-only",
        help="Print the launch details and guarded prompt without starting Codex",
    ),
):
    """Launch a compatibility agent in read-only planning mode."""
    try:
        target = resolve_target_repo(repo_root)
        if executor is Executor.COPILOT:
            prompt = build_copilot_prompt(task, target)
        else:
            prompt = build_codex_prompt(task, target)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    console.print(f"[bold cyan]Repository:[/bold cyan] {target}")
    console.print(f"[bold cyan]Executor:[/bold cyan] {executor.value}")

    if executor is Executor.COPILOT:
        console.print(
            Panel(
                prompt,
                title="Paste into VS Code Copilot Agent mode",
                border_style="blue",
            )
        )
        return

    try:
        if executor is Executor.CLAUDE:
            command = build_claude_command(task, target)
        else:
            command = build_codex_command(task, target)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    if print_only:
        preview = [*command[:-1], "<guarded workflow prompt>"]
        console.print(f"[bold cyan]Command:[/bold cyan] {shlex.join(preview)}")
        console.print(Panel(prompt, title=f"Initial {executor.value} prompt", border_style="blue"))
        return

    try:
        guard_external_agent_prompt(command[-1], target)
    except (RuntimeError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    agent_name = executor.value.capitalize()
    console.print(
        f"[dim]Launching {agent_name} in read-only planning mode. Use the separate "
        "approve and execute commands after review.[/dim]"
    )
    run_kwargs: dict = {}
    if executor is Executor.CLAUDE:
        run_kwargs["cwd"] = str(target)
    completed = subprocess.run(command, check=False, **run_kwargs)
    if completed.returncode:
        raise typer.Exit(completed.returncode)


@app.command("plan")
def plan_command(
    prompt: str = typer.Argument(..., help="Coding task to plan without implementing"),
    file: Path = typer.Option(
        None,
        "--file",
        "-f",
        help="Safe repository file to include as additional context",
    ),
    repo_root: Path = typer.Option(
        None,
        "--repo-root",
        "-r",
        help="Target repository or a directory inside it; defaults to the current directory",
    ),
    allowed_paths: list[str] = typer.Option(
        None,
        "--allow",
        help="Repository-relative allowed write path; repeat to create a plan record",
    ),
    effective_constraints: str = typer.Option(
        DEFAULT_EFFECTIVE_CONSTRAINTS,
        "--constraints",
        help="Sanitized constraints shared by planning and execution",
    ),
):
    """Generate a read-only plan; --allow creates a verifiable plan record."""
    try:
        target = resolve_target_repo(repo_root)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    console.print(f"[bold cyan]Repository:[/bold cyan] {target}")
    console.print("[dim]Generating a read-only plan...[/dim]")
    if allowed_paths:
        structured = run_structured_plan(
            prompt,
            context_path=str(file) if file else None,
            repo_root=str(target),
            allowed_paths=allowed_paths,
            effective_constraints=effective_constraints,
        )
        plan_path = save_plan(structured)
        proposal = structured.proposal
        console.print(f"[bold green]Plan ID:[/bold green] {structured.plan_id}")
        console.print(f"[bold green]Plan record:[/bold green] {plan_path}")
        console.print(
            "[dim]Review the proposal and record, then run "
            f"`orchestrate approve {shlex.quote(str(plan_path))}`.[/dim]"
        )
    else:
        proposal = run_plan(
            prompt,
            context_path=str(file) if file else None,
            repo_root=str(target),
            effective_constraints=effective_constraints,
        )
    console.print(
        Panel(
            Markdown(proposal),
            title="[bold yellow]Plan (no changes made)[/bold yellow]",
            border_style="yellow",
        )
    )


@app.command("approve")
def approve_command(
    plan_file: Path = typer.Argument(..., help="Structured plan JSON to approve"),
    approved_by: str = typer.Option(
        None,
        "--approved-by",
        help="Approver identity; defaults to the current OS user",
    ),
):
    """Create an explicit, single-use approval for an exact plan."""
    import getpass

    try:
        structured = load_plan(plan_file)
        target = resolve_target_repo(structured.repository.repo_root)
        snapshot = capture_repository_snapshot(target)
        policy = reload_policy_identity(
            structured.policy.sources, structured.effective_constraints
        )
        validate_plan_state(
            structured,
            current_base_commit=snapshot.base_commit,
            current_working_tree_fingerprint=snapshot.working_tree_fingerprint,
            current_policy_fingerprint=policy.fingerprint,
        )
        approval = create_approval(structured, approved_by or getpass.getuser())
        approval_path = save_approval(approval)
    except (OSError, TypeError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"[bold green]Approved plan:[/bold green] {structured.plan_id}")
    console.print(f"[bold green]Approval record:[/bold green] {approval_path}")
    console.print("[yellow]Approval is single-use and will be consumed at execution.[/yellow]")


@app.command("execute")
def execute_command(
    plan_file: Path = typer.Argument(..., help="Approved structured plan JSON"),
    approval_file: Path = typer.Argument(..., help="Matching approval JSON"),
    executor: Executor = typer.Option(
        Executor.CODEX,
        "--executor",
        "-e",
        help="Write-capable executor to launch",
        case_sensitive=False,
    ),
    print_only: bool = typer.Option(
        False,
        "--print-only",
        help="Validate and print launch details without consuming approval or executing",
    ),
):
    """Validate approval and drift, then launch a separate execution session."""
    try:
        structured = load_plan(plan_file)
        approval = load_approval(approval_file)
        validate_approval_location(approval_file, approval)
        target = resolve_target_repo(structured.repository.repo_root)
        before = capture_repository_snapshot(target)
        policy = reload_policy_identity(
            structured.policy.sources, structured.effective_constraints
        )
        validate_approval(
            structured,
            approval,
            current_base_commit=before.base_commit,
            current_working_tree_fingerprint=before.working_tree_fingerprint,
            current_policy_fingerprint=policy.fingerprint,
        )
        command = build_execution_command(structured, executor)
    except (OSError, TypeError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    if print_only:
        preview = [*command[:-1], "<approved execution prompt>"]
        console.print(f"[bold cyan]Command:[/bold cyan] {shlex.join(preview)}")
        console.print(
            Panel(
                build_execution_prompt(structured),
                title="Approved execution prompt",
                border_style="yellow",
            )
        )
        return

    try:
        guard_external_agent_prompt(command[-1], target)
    except (RuntimeError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    before_metadata = capture_path_metadata(target, before.changed_paths)
    try:
        consume_approval(approval_file, approval)
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    console.print(
        f"[dim]Launching {executor.value}; approval {approval.approval_id} is now consumed.[/dim]"
    )
    run_kwargs: dict = {}
    if executor is Executor.CLAUDE:
        run_kwargs["cwd"] = str(target)
    completed = subprocess.run(command, check=False, **run_kwargs)

    after = capture_repository_snapshot(target)
    metadata_paths = tuple(sorted(set(before.changed_paths) | set(after.changed_paths)))
    after_metadata = capture_path_metadata(target, metadata_paths)
    try:
        changed = validate_execution_result(
            structured,
            before,
            after,
            before_metadata=before_metadata,
            after_metadata=after_metadata,
        )
    except ValueError as exc:
        console.print(f"[bold red]Scope validation failed:[/bold red] {exc}")
        raise typer.Exit(2) from exc

    console.print(
        "[bold green]Approved scope validated.[/bold green] "
        + (", ".join(sorted(changed)) if changed else "No Git-visible changes.")
    )
    if completed.returncode:
        raise typer.Exit(completed.returncode)


@app.command()
def ask(
    prompt: str = typer.Argument(..., help="Your question or task"),
    file: Path = typer.Option(None, "--file", "-f", help="Path to a file to include as context"),
    no_judge: bool = typer.Option(False, "--no-judge", help="Skip the critique/revision pass"),
    plan_only: bool = typer.Option(
        False,
        "--plan",
        help="Show a plan and ask for confirmation before generating advice",
    ),
    repo_root: Path = typer.Option(
        None,
        "--repo-root",
        help="Target Git repository; loads effective AGENTS.md and scopes RAG",
    ),
):
    """Send a prompt through the full router → specialist → judge pipeline."""
    ctx_path = str(file) if file else None

    # Compatibility plan-first mode for an advisory response.
    if plan_only or os.getenv("PLAN_FIRST", "false").lower() == "true":
        console.print("[dim]Generating plan...[/dim]")
        proposal = run_plan(
            prompt,
            context_path=ctx_path,
            repo_root=str(repo_root) if repo_root else None,
        )
        console.print(Panel(Markdown(proposal), title="[bold yellow]Plan (no changes made)[/bold yellow]", border_style="yellow"))
        approved = typer.confirm("\nProceed to the advisory response?", default=False)
        if not approved:
            console.print("[dim]Aborted — no changes made.[/dim]")
            raise typer.Exit()
        console.print("[dim]Confirmed. Generating the advisory response...[/dim]")

    console.print("[dim]Classifying prompt...[/dim]")
    result = run(
        prompt,
        context_path=ctx_path,
        judge_enabled=False if no_judge else None,
        repo_root=str(repo_root) if repo_root else None,
        effective_constraints=DEFAULT_EFFECTIVE_CONSTRAINTS,
    )

    console.print(f"[bold cyan]Status:[/bold cyan] {result.get('status', 'success')}")
    for warning in result.get("warnings", []):
        console.print(
            "[yellow]Warning "
            f"({warning['component']}/{warning['code']}): {warning['message']}[/yellow]"
        )
    if not result["final"]:
        error = result.get("error") or {
            "code": "unknown_failure",
            "message": "The request did not produce an answer.",
        }
        console.print(
            f"[bold red]Error ({error['code']}):[/bold red] {error['message']}"
        )
        raise typer.Exit(2)

    console.print(f"[bold cyan]Task type:[/bold cyan] {result['task_type']}")
    console.print(
        f"[bold cyan]RAG context:[/bold cyan] "
        f"{'yes' if result.get('retrieval_used', result['context_used']) else 'no'}"
    )

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
    from orchestrator.rag import index_directory

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
    from orchestrator.rag import scan_directory

    _, report = scan_directory(str(source))
    console.print(report.summary(audit=True))


if __name__ == "__main__":
    app()
