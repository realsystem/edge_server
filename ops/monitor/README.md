# Off-site heartbeat monitor (Uptime Kuma on a VM)

Edge sites (a Frigate node, a Home Assistant box, a farm controller) send a heartbeat every few minutes. This monitor
receives it somewhere that is **not on the site**, so it still works when the site loses power, its uplink or its
controller, and it alerts you when the heartbeats stop. It is [Uptime Kuma](https://github.com/louislam/uptime-kuma) (a
"Push" monitor) behind [Caddy](https://caddyserver.com/) for automatic HTTPS (your existing Caddy, or one the script
starts), in Docker, on any Debian or Ubuntu VM. A free GCP e2-micro is enough (check Google's current free-tier terms
for regions and limits).

## Deploy (about 10 minutes)

1. **Create the VM**: Compute Engine, `e2-micro`, Debian 12 or Ubuntu 24.04, a free-tier region (for example
   `us-central1`), 10-30 GB standard disk. Under *Networking* tick *Allow HTTP* and *Allow HTTPS* (or use the firewall
   commands the script prints at the end). Optionally reserve a static external address.
2. **Make a free watchdog check** (so a dead VM is not silent): sign up at healthchecks.io, create a check with period
   5 minutes and grace 15 minutes, and copy its ping URL (`https://hc-ping.com/<uuid>`).
3. **On the VM** (browser SSH from the console is fine). This repository is public, so no key is needed:
   ```
   sudo apt-get update && sudo apt-get install -y git
   git clone https://github.com/realsystem/edge_server.git
   cd edge_server
   sudo ./ops/monitor/deploy.sh --admin-ip <your home IP>/32 --watchdog-url https://hc-ping.com/<uuid>
   ```
   Find your address at https://api.ipify.org. Add `--domain monitor.example.com` if you have a domain (an A record
   must point at the VM); without it the script uses `<address>.sslip.io`, which needs no DNS. (To clone a *private*
   repo on the VM instead, see [`ops/vm/setup-github-key.sh`](../vm/setup-github-key.sh).)
4. **Do what the script prints at the end.** Kuma 2's first screen asks for a database: choose **SQLite** (MariaDB needs far
   more memory than a small VM has). Then create the admin account at once, add a Telegram or Pushover notification, add a
   *Push* monitor, point your heartbeat sender at its URL, and test. The first start of Kuma can take several minutes on a
   small VM (the image is large); the script waits for it before it finishes. A `403` from its own final check is normal when
   `--admin-ip` is set, because the check runs from the VM's own address.

The script installs swap (if the VM is small), Docker, automatic security updates, the monitor, a nightly backup and the
optional watchdog ping. Run it again to update or to change an option; your data and earlier options are kept.

## What it sets up

| Thing | Where |
|---|---|
| Compose file, `.env` (mode 600), data (and our own `Caddyfile` in docker mode) | `/opt/edge-monitor/` |
| Nightly backup of Kuma's data (14 days kept) | `/var/backups/edge-monitor/`, cron `03:17`, `sudo edge-monitor-backup` |
| Watchdog ping to your external check, every 5 min, only while healthy | `/etc/cron.d/edge-monitor-watchdog` |
| Public ports | 80 and 443, served by Caddy (yours, or ours in docker mode). Kuma listens on `127.0.0.1:3001` only. |

## A Caddy already runs on the VM

The script never starts a second Caddy on ports that one holds. `--caddy auto` (the default) looks for a running `caddy`
service and, if there is one, **appends a marked block to its Caddyfile** and reloads it:

- Mode `host`: the block `monitor.<ip>.sslip.io { ... reverse_proxy 127.0.0.1:3001 }` goes at the end of the file, between
  `# BEGIN edge-monitor` and `# END edge-monitor`. Everything else in the Caddyfile is left exactly as it was.
- Before editing it makes a backup next to the file (`Caddyfile.edge-monitor-backup-<time>`), checks the result with
  `caddy validate`, then runs `systemctl reload caddy`. If validation or the reload fails, the original is put back.
- Running the script again refreshes that block (for example a new `--admin-ip`); it never adds a second one.
- It refuses if the Caddyfile already serves the chosen host name, or if the markers of an earlier run were damaged.
- The default name has a `monitor.` prefix (for example `monitor.203-0-113-10.sslip.io`): sslip.io resolves it to the same
  VM, so it cannot clash with the site your Caddy already serves. Use `--domain` for a name of your own.
- To remove it: delete the lines from `# BEGIN edge-monitor` to `# END edge-monitor`, then `sudo systemctl reload caddy`.
- If Caddy uses a different file, pass `--caddyfile /path/to/Caddyfile`.

Other modes: `--caddy docker` (our own Caddy container; needs ports 80 and 443 free) and `--caddy none` (only Kuma on
loopback; the script prints the block to put into whatever proxy you use, for example Caddy in Docker or nginx).

With `--admin-ip`, only those addresses can open the admin UI. The heartbeat URL (`/api/push/<secret>`) stays open to
everyone because a site's public address changes (Starlink, mobile data); the secret in the URL is what protects it, so
keep it private and HTTPS-only.

## Setting Kuma up from code

Instead of clicking through Kuma's pages, `ops/monitor/kuma-provision.sh` (a wrapper around `tools/kuma_provision.py`) does the
first-run work and can be run again at any time. It chooses SQLite, creates the admin account, adds the Telegram notification,
and makes one push monitor per site, saving each monitor's push URL to a private file. Run it on the VM, against Kuma's
loopback address (the admin page of a public monitor is normally limited to your own address):

```
# secrets live in files only you can read; the admin password file is created for you if it does not exist
sudo ./ops/monitor/kuma-provision.sh --plan \
    --url http://127.0.0.1:3001 --public-url https://monitor.example.com \
    --admin-password-file /root/.kuma-admin \
    --telegram-token-file /root/.kuma-telegram-token --telegram-chat-id 123456789 \
    --monitor "Home HA heartbeat" --monitor "Farm HA heartbeat" --urls-out /root/heartbeat-urls.env
```

Drop `--plan` to apply. The push URLs are written as `HOME_HA_HEARTBEAT_URL=...` lines (mode 600) and **never printed**: copy
each into its sender (an HA helper, a host's heartbeat file). Running it again changes nothing that is already right; a changed
`--interval` or a new `--monitor` is applied. The Telegram step sends a test message first and stops if it fails (a bot can only
message a chat that has started a conversation with it). The Socket.IO client library is installed into a private venv, not
system-wide. Kuma has no setup API, so this drives the same channel its web page uses; a Kuma upgrade could change it.

## Heartbeat details to know

- A sender may use POST or GET. Uptime Kuma 2 accepts any method on the push URL; version 1 only accepted GET. The script
  defaults to the `:2` image. If the monitor stays "pending" and the sender logs 404 or 405, the image is too old: pass
  `--image louislam/uptime-kuma:2`, or send with GET.
- Kuma stores only the `msg` text of a push (`?status=up&msg=...`), not a JSON body, so put a one-line summary in the URL if
  you want it in the history. Self-hosted Healthchecks stores POST bodies instead.
- Alert delivery is Kuma's job, so set up a notification channel that reaches your phone and press *Test*.

## Costs and limits

- The VM and the Let's Encrypt certificate can be free; Google's free tier has caps (region, disk, outbound traffic) that
  change, so check them. Healthchecks.io's free plan has a check limit.
- One VM is a single point of failure: that is what the watchdog ping is for.

## Not covered

Authentication on the push URL beyond its secret, multi-region monitoring, restoring from a backup (stop the
containers, untar `kuma-<date>.tgz` into `/opt/edge-monitor/data/`, start them).

## Verified how

`bash -n`, shellcheck, the Caddyfile generation (`./ops/monitor/deploy.sh --print-caddyfile`) and `tests/test_monitor.py`,
which runs the real Caddyfile edit against a temporary file with stub `caddy` and `systemctl`. The install itself needs
a VM: report the first run's output if anything fails.
