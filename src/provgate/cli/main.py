"""provgate command-line interface (thin frontend over the core)."""

from __future__ import annotations

import time

import typer

from provgate.config import load_settings
from provgate.store.crypto import generate_key
from provgate.store.models import AssignmentPolicy, SecretKind
from provgate.sync.engine import sync_all, sync_class
from provgate.sync.loop import run_loop

from .wiring import open_repo, real_gs_login, real_prov, utc_now_iso

app = typer.Typer(help="Sync Gradescope submissions into Provenance.")
class_app = typer.Typer(help="Manage class configurations.")
app.add_typer(class_app, name="class")


@app.command()
def keygen() -> None:
    """Print a fresh master key for PROVGATE_SECRET_KEY."""
    typer.echo(generate_key())


@class_app.command("add")
def class_add(
    label: str = typer.Option(...),
    gradescope_course: str = typer.Option(...),
    gradescope_email: str = typer.Option(...),
    provenance_base_url: str = typer.Option(...),
    provenance_semester: str = typer.Option(...),
    assignments: str = typer.Option("all", help="all | include:1,2 | exclude:3"),
) -> None:
    """Register a class. Gradescope password + Provenance token are prompted (never flags)."""
    policy = AssignmentPolicy.parse(assignments)
    gs_pw = typer.prompt("Gradescope password", hide_input=True)
    token = typer.prompt("Provenance API token", hide_input=True)
    repo = open_repo(load_settings())
    cfg = repo.add_class(
        label=label,
        gradescope_course_id=gradescope_course,
        gradescope_email=gradescope_email,
        provenance_base_url=provenance_base_url,
        provenance_semester_id=provenance_semester,
        assignment_policy=policy,
    )
    repo.set_secret(cfg.id, SecretKind.GRADESCOPE_PASSWORD, gs_pw)
    repo.set_secret(cfg.id, SecretKind.PROVENANCE_TOKEN, token)
    typer.echo(f"added class {label!r}")


@class_app.command("list")
def class_list() -> None:
    repo = open_repo(load_settings())
    for c in repo.list_classes():
        flag = "" if c.enabled else " (disabled)"
        typer.echo(
            f"{c.label}\tcourse={c.gradescope_course_id}\tpolicy={c.assignment_policy.serialize()}{flag}"
        )


@class_app.command("remove")
def class_remove(label: str) -> None:
    open_repo(load_settings()).remove_class(label)
    typer.echo(f"removed {label!r}")


@app.command()
def sync(
    label: str = typer.Option(None, "--class", help="Sync one class (default: all enabled)."),
    all_classes: bool = typer.Option(
        False, "--all", help="Sync all enabled classes (the default when --class is omitted)."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Compute the delta but POST nothing."),
    loop: bool = typer.Option(False, "--loop", help="Repeat forever on an interval."),
    interval: float = typer.Option(3600.0, "--interval", help="Loop interval in seconds."),
) -> None:
    if label and all_classes:
        raise typer.BadParameter("specify either --class or --all, not both")

    settings = load_settings()
    repo = open_repo(settings)
    prov = real_prov(settings)
    login = real_gs_login(settings.http_timeout_s)

    def _once() -> None:
        if label:
            cfg = repo.get_class(label)
            if cfg is None:
                raise typer.BadParameter(f"no class named {label!r}")
            results = {
                label: sync_class(repo, login, prov, cfg, now_iso=utc_now_iso, dry_run=dry_run)
            }
        else:
            results = sync_all(repo, login, prov, now_iso=utc_now_iso, dry_run=dry_run)
        for lbl, outcomes in results.items():
            for o in outcomes:
                typer.echo(f"{lbl}\t{o.gs_assignment_id}\t{o.outcome}\tdelta={o.delta_count}")

    if loop:
        run_loop(_once, interval, sleep=time.sleep)
    else:
        _once()


@app.command()
def runs(limit: int = typer.Option(20)) -> None:
    repo = open_repo(load_settings())
    for r in repo.recent_runs(limit):
        typer.echo(
            f"{r.finished_at}\tclass={r.class_id}\t{r.gs_assignment_id}\t{r.outcome}\tdelta={r.delta_count}"
        )
