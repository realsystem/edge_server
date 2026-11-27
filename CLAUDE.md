# Edge Server

Deploy scripts and a Telegram bot for unattended Frigate NVR + Tailscale nodes (Ubuntu, Debian, OpenMediaVault; optional Home Assistant, MQTT, BLE power plugins) and for Home Assistant OS nodes (add-on). Used by operators running one node or a fleet of sites. See README.md and `docs/design/edge-system.md`.

## Commands
- Install: `pip install -e ".[dev]"` (or `make setup`, which makes `.venv`). It pulls the pinned `autonomy-core` from GitHub; the tests import it. (unverified: PyPI was blocked here; I ran the tests with the pinned `autonomy-core` source on `PYTHONPATH`)
- All Python tests: `pytest` (about 4 min, 1196 passed, 2 skipped). One file: `pytest tests/test_fleet.py`. One test: `pytest tests/test_fleet.py::TestCommandLine::test_check_ok_and_error_exit_codes`. Works without an editable install (`pythonpath = src, tools`).
- Shell tests: `./tests/test-scripts.sh` (under 1 s; run it as a normal user, the "refuses without root" checks need that).
- Lint, as CI runs it (black, ruff and mypy verified clean here; shellcheck not installed, unverified): `black --check src tests tools telegram-bot`; `ruff check src tests tools telegram-bot`; `mypy src tools telegram-bot/bot.py telegram-bot/controlplane.py --ignore-missing-imports`; `make lint` (shellcheck).
- Everything except Docker jobs: `make ci-local` (unverified).
- Plugins are separate packages: `cd plugins/<name> && pip install -e ".[dev]" && pytest` (unverified).
- Needs a Docker daemon (unverified, none here): `make test`, `lab/lab.sh up --mock-telegram` (add `--nodes` for the Home Assistant add-on node), `make ha-test`.
- Deploy: `./deploy-edge-server.sh --config fleet.yaml --node NAME --check` (read-only, no root); `sudo ./deploy-edge-server.sh [--auto|--classic]`; `python3 tools/ha_node.py {init,check,plan,backup,sync}`. There is no build step.

## Architecture
- `deploy-edge-server.sh`: only the outline. It loads `lib/*.sh` and runs the steps in order. `lib/` has one file per area (`settings.sh`/`fleet.sh`/`config.sh` for where settings come from, `os/ubuntu.sh` and `os/debian.sh`, `platform.sh` for OpenMediaVault, `stack.sh`, `telegram.sh`, `redeploy.sh`, ...).
- `telegram-bot/`: the bot, run in a container next to Frigate. `bot.py` is the main loop and commands, `controlplane.py` the signed node-to-node heartbeats and `/fleet`, `governance.py` policy and audit for `/restart` `/reboot` `/redeploy`, `peercmd.py` `/remote`, `assistant.py` `/ask`, `farm.py` the farm status. `service-request.py`, `smart-report.py` and `host-snapshot.sh` run on the host.
- `tools/`: `fleet.py` reads and checks `fleet.yaml` for the deploy script; `ha_node.py` configures a HAOS node over SSH and the HA API; `camera_discovery.py`; `kuma_provision.py` sets up an Uptime Kuma server (admin, Telegram notification, push monitors) over its Socket.IO channel and is tested against a fake Kuma (`ops/monitor/kuma-provision.sh` runs it with its dependency in a venv).
- `src/`: flat modules of the `edge-bootstrap` CLI (`bootstrap.py`, `ssh.py`, `state.py`, `secret_store.py`, ...).
- `ops/`: helpers that run on a VM, Linux only. `ops/monitor/` (`deploy.sh`, compose file, README) deploys the off-site heartbeat monitor (Uptime Kuma; Caddy in front, either an existing Caddy service whose Caddyfile it appends a marked block to, or its own container). `ops/heartbeat/` has the generic senders (host systemd timer `install.sh` + `node-heartbeat.sh`, and the Home Assistant package `ha/edge_heartbeat.yaml`), tested in `tests/test_heartbeat_senders.py` with stub `curl`/`df` and the templates rendered under Jinja. `ops/vm/setup-github-key.sh` gives a VM a per-repo read-only GitHub deploy key and is meant to be copied into other repos. Tests: `tests/test_monitor.py` (runs the real Caddyfile edit against stub `caddy` and `systemctl`) and `tests/test_vm_scripts.py` (key and ssh-config parts skip on non-Linux).
- `host/` and `lib/protect.sh`: opt-in host protection, off by default: `CONFIG_BACKUP_DIR` (nightly config archive, `host/config-backup.sh`) and `MD_ALERT=true` (alert program for the mdadm monitor, `host/md-alert.sh`, only where an array exists and `mdadm.conf` has no target). Tests: `tests/test_host_protection.py`.
- `ha-addon/`: the bot as a Home Assistant add-on (Docker build context is the repo root). `plugins/`: `nut-power`, `victron-shunt`, `renogy-rover`. `lab/`: the Docker lab. `examples/`: `fleet.minimal.yaml`, `fleet.two-site.yaml`, `fleet.schema.json`.
- Flow: the deploy script resolves settings (flags, then env, then the fleet file, then the last `deploy.env`, then defaults), writes the compose file, systemd units and bot state under `/opt/edge-server`. The bot never runs host commands itself: it drops request files in `bot-requests/` and host systemd path units act on them. Fleet nodes exchange signed messages on `fleet.control_port` (default 7443) over Tailscale and the LAN.

## Conventions
- Python 3.10 to 3.13; black and ruff line length 100; `mypy` with `disallow_untyped_defs`. CI fails under 85% coverage for `src` and `tools`, 75% for each plugin.
- Shell is shellcheck clean with the exclusions in `make lint`; `lib/*.sh` are sourced, never executed.
- The deploy script stays idempotent, and the default "classic" mode (no `--config`) must keep behaving exactly as before. Node and site names are labels: never use them in paths, container names or systemd unit names.
- This repository is **public**. Never commit or paste into a PR: names of private repositories, real host names or public addresses of your sites, tokens, push URLs, Telegram bot names or chat ids, passwords, or personal data. Examples use `acme/webapp`, `example.com`, `203.0.113.x` and `<uuid>`. Secrets go through `secrets.sh` or a git-ignored file on the machine that needs them. A private repository may refer to this one, not the other way round (the older mentions of `farm_automation` in `docs/design/ha-node.md` and `tests/test_ha_node.py` are the exception).
- Commits: a short subject with an area prefix (`ha_node:`, `feat:`, `docs:`, `Lab:`) and the PR number; PRs are squash-merged to `main` after all CI jobs pass.

## Gotchas
- The `autonomy-core` commit is pinned in four places: `pyproject.toml`, `requirements.txt`, `telegram-bot/Dockerfile` (`AUTONOMY_CORE_REF`) and `ha-addon/Dockerfile`. A test checks only the first three. `lab/lab.sh` expects the `autonomy` repo next to this one (or use `--pinned`).
- Never commit `.telegram`, `.cam`, `.env`, `*.enc` or `deploy.env`. CI runs `autonomy secrets scan . --allowlist .secrets-allowlist` and gitleaks; test data that looks like a secret goes in `.secrets-allowlist`.
- `/redeploy` installs only the newest release tag (`vX.Y`) that has green CI, so a merge to `main` reaches no running node until a tag is cut (newest on 23 Nov 2026: v1.2, from 2 Nov).
- On OpenMediaVault the deploy leaves firewall, mounts, time zone and updates to OMV on purpose (`lib/platform.sh`).
- The full pytest run is about 4 minutes with no single slow test; run per file while iterating.
- A weekly CI run pulls `frigate:stable`, so a new Frigate release can break "Frigate accepts generated configs" without any code change.
- Add-on Supervisor role: `hassio_role: manager` may need `admin` for `/host/reboot`; not confirmed on a real node (`ha-addon/DOCS.md`).
- Docs lag the code: README lines 8 to 11 ("not implemented yet") and the status line of `docs/design/edge-system.md` (lists `ha-addon` as not built) are stale. Trust the code and README's "Fleet file" section.

## Definition of done
- `pytest` and `./tests/test-scripts.sh` pass; `black --check`, `ruff check`, `mypy` (CI's file list, not `make ci-local`'s shorter one) and shellcheck are clean.
- For bot, compose or deploy changes: `lab/lab.sh up --mock-telegram` still works, or the Docker CI jobs pass.
- All CI checks green on the PR, including the secret scan.
- README and the status line of `docs/design/edge-system.md` updated when behavior or phases change.

## GitHub Free-plan limits
This repo is public, so GitHub Actions minutes are free here. The account is on the GitHub Free plan and these limits still apply.

- **Workflows:** keep jobs lean. Use `ubuntu-latest`, add `concurrency` with `cancel-in-progress: true` and `timeout-minutes` to new jobs, and set `retention-days` (3-7) on uploaded artifacts. The account-wide cap is 20 concurrent jobs (5 macOS); extra jobs queue.
- **Files:** never commit a file over 50 MiB (GitHub warns) or 100 MiB (the push is blocked). Keep datasets, video, model weights and build output out of git; use Git LFS only if truly needed.
- **GitHub API:** 5,000 requests/hour authenticated (60 unauthenticated), search 30/min, plus secondary limits. Scripts or agents that call the API must paginate, use conditional requests, and not fan out across repos in parallel.
