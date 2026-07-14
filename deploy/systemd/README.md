# systemd deployment

Run `provgate sync --all` on a schedule as a **oneshot service driven by a timer** —
the one-pass-then-exit model the CLI is designed for, with no long-lived process and
no cron-inside-the-app. These units run `provgate` directly on the host with `uv`; they
do not use the container image.

## Files

| File                    | Purpose                                                        |
| ----------------------- | ------------------------------------------------------------- |
| `provgate.service`      | `Type=oneshot` — runs one `provgate sync --all` pass.         |
| `provgate.timer`        | Triggers the service hourly (edit `OnCalendar` to taste).     |
| `provgate.env.example`  | Template for the secrets/overrides env file (never commit the real one). |

## Install (user units)

```bash
# 1. Checkout + deps, from the repo root:
uv sync

# 2. Secrets: create the env file the service reads, and lock it down.
mkdir -p ~/.config/provgate
cp deploy/systemd/provgate.env.example ~/.config/provgate/provgate.env
$EDITOR ~/.config/provgate/provgate.env          # set PROVGATE_SECRET_KEY (uv run provgate keygen)
chmod 600 ~/.config/provgate/provgate.env

# 3. Install the units and start the timer.
mkdir -p ~/.config/systemd/user
cp deploy/systemd/provgate.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now provgate.timer

# 4. So user units keep running while you're logged out:
loginctl enable-linger "$USER"
```

Before editing the units, check the two host-specific paths in `provgate.service`:

- `WorkingDirectory` — where you cloned the repo (default `~/provenance-gradescope-gateway`).
- `ExecStart` — the path to your `uv` binary (default `~/.local/bin/uv`; run `which uv`).

`PROVGATE_DB_PATH` is handled for you: `StateDirectory=provgate` creates and owns
`~/.local/state/provgate/`, and the unit points the SQLite store there. Register your
classes (`uv run provgate class add …`) with the **same** `PROVGATE_SECRET_KEY` and
`PROVGATE_DB_PATH` the service uses, so it can decrypt what you stored.

## Operate

```bash
systemctl --user list-timers provgate.timer   # when it next fires / last ran
systemctl --user start provgate.service       # run a pass right now
journalctl --user -u provgate.service -f      # follow logs
```

## System (root) units instead of user units

Drop the same files in `/etc/systemd/system/`, add `User=` / `Group=` and a real
`WorkingDirectory` to the service, use `systemctl` without `--user`, and skip the
linger step. `%h`/`%S` still resolve correctly (to the service user's home and
`/var/lib/provgate`).
