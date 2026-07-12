"""provgate command-line interface (thin frontend over the core)."""

from __future__ import annotations

import logging
import time

import typer

from provgate.config import load_settings
from provgate.notify.render import render_summary
from provgate.notify.webhook import post_summary
from provgate.store.crypto import generate_key
from provgate.store.models import AssignmentPolicy, SecretKind
from provgate.sync.engine import sync_all, sync_class
from provgate.sync.loop import run_loop
from provgate.sync.policy import resolve_assignments

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


@class_app.command("edit")
def class_edit(
    label: str = typer.Argument(...),
    gradescope_course: str = typer.Option(None, help="New Gradescope course id."),
    gradescope_email: str = typer.Option(None, help="New Gradescope staff email."),
    provenance_base_url: str = typer.Option(None, help="New Provenance API base URL."),
    provenance_semester: str = typer.Option(None, help="New Provenance semester id."),
    assignments: str = typer.Option(
        None, help="New assignment scope: all | include:1,2 | exclude:3"
    ),
    enabled: bool = typer.Option(
        None, "--enable/--disable", help="Enable or disable the class (default: unchanged)."
    ),
    rotate_gs_password: bool = typer.Option(
        False, "--rotate-gs-password", help="Prompt for a new Gradescope password."
    ),
    rotate_token: bool = typer.Option(
        False, "--rotate-token", help="Prompt for a new Provenance API token."
    ),
) -> None:
    """Edit a class's config. Secrets are rotated via --rotate-* flags, never passed as flags."""
    repo = open_repo(load_settings())
    cfg = repo.get_class(label)
    if cfg is None:
        raise typer.BadParameter(f"no class named {label!r}")
    policy = AssignmentPolicy.parse(assignments) if assignments is not None else None
    repo.update_class(
        label,
        gradescope_course_id=gradescope_course,
        gradescope_email=gradescope_email,
        provenance_base_url=provenance_base_url,
        provenance_semester_id=provenance_semester,
        assignment_policy=policy,
        enabled=enabled,
    )
    if rotate_gs_password:
        gs_pw = typer.prompt("New Gradescope password", hide_input=True)
        repo.set_secret(cfg.id, SecretKind.GRADESCOPE_PASSWORD, gs_pw)
    if rotate_token:
        token = typer.prompt("New Provenance API token", hide_input=True)
        repo.set_secret(cfg.id, SecretKind.PROVENANCE_TOKEN, token)
    typer.echo(f"updated class {label!r}")


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
def doctor(
    label: str = typer.Option(..., "--class", help="Class to check."),
) -> None:
    """Verify a class's Gradescope + Provenance credentials without syncing anything."""
    settings = load_settings()
    repo = open_repo(settings)
    cfg = repo.get_class(label)
    if cfg is None:
        raise typer.BadParameter(f"no class named {label!r}")

    ok = True

    try:
        gs_pw = repo.get_secret(cfg.id, SecretKind.GRADESCOPE_PASSWORD)
        client = real_gs_login(settings.http_timeout_s)(cfg.gradescope_email, gs_pw)
        try:
            assignments = client.list_assignments(cfg.gradescope_course_id)
            in_scope = resolve_assignments(cfg.assignment_policy, [a.id for a in assignments])
        finally:
            client.close()
    except Exception as e:  # noqa: BLE001 - report as a doctor check, never leak the password
        ok = False
        typer.echo(f"Gradescope: FAIL ({e})")
    else:
        typer.echo(f"Gradescope: ok ({len(assignments)} assignment(s), {len(in_scope)} in scope)")

    try:
        token = repo.get_secret(cfg.id, SecretKind.PROVENANCE_TOKEN)
        verified = real_prov(settings).verify_token(cfg.provenance_base_url, token)
    except Exception as e:  # noqa: BLE001 - report as a doctor check, never leak the token
        ok = False
        typer.echo(f"Provenance: FAIL ({e})")
    else:
        if verified:
            typer.echo("Provenance: ok (token valid)")
        else:
            ok = False
            typer.echo("Provenance: FAIL (token rejected)")

    if not ok:
        raise typer.Exit(code=1)


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

        if settings.webhook_url:
            try:
                content = render_summary(results, now_iso=utc_now_iso(), dry_run=dry_run)
                post_summary(settings.webhook_url, content, timeout_s=settings.webhook_timeout_s)
            except Exception:  # noqa: BLE001 — notify must never affect sync outcome
                logging.getLogger("provgate.notify").warning(
                    "failed to render/post webhook summary", exc_info=True
                )

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
