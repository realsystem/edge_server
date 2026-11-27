# External heartbeat monitoring: being told when a site cannot tell you itself

**Status:** Built, tested in pieces and end to end on a test site · 2026-11-27

## The problem

A Home Assistant box, a NAS or a video server can alert you about almost anything, except about itself being down: a dead power
supply, a crashed Pi, a failed router or a lost uplink silences the thing that would have sent the alert. The only reliable
signal is the **absence** of a signal, noticed by something outside the site.

## The design in one picture

```
 site A (home HA) ──every 5 min──┐
 site B (farm HA) ──every 5 min──┼──> monitor on a VM, outside every site ──> Telegram on your phone
 host C (NAS)     ──every 5 min──┘            ▲
                                              └── the VM pings a free hosted check every 5 min (watches the watcher)
```

| Part | What it is | Where it lives |
|---|---|---|
| **Sender** | Posts a heartbeat every 5 minutes. Generic ones: a systemd timer for any Linux host, a package for any Home Assistant. A site may have its own richer sender (sensor health, a controller) | `ops/heartbeat/` here; site-specific senders in that site's own (private) repository |
| **Receiver** | Uptime Kuma, one **Push monitor per site**. It goes Down when no beat arrives for 15 minutes (three missed beats, no retries) | `ops/monitor/deploy.sh` |
| **Alert path** | Kuma sends to Telegram itself, so it works when the site, its network and its Home Assistant are all dead | set up by `tools/kuma_provision.py` |
| **Watchdog** | The VM pings a free hosted check every 5 minutes while Kuma and its web front are healthy; if the VM dies, that service tells you | `deploy.sh --watchdog-url` |
| **Provisioning** | Database choice, admin account, notification and monitors as code, safe to repeat | `tools/kuma_provision.py`, `ops/monitor/kuma-provision.sh` |
| **Delivery to nodes** | Home Assistant packages are put on a node with `tools/ha_node.py sync`; `--reload` for a small node | `tools/ha_node.py` |

Each sender gets its **own** monitor and URL, so a test site's noise never reaches a production site's alert history, and an
alert names the site ("[Home HA heartbeat] [Down]").

## What a sender sends

For an Uptime Kuma push URL (it contains `/api/push/`) the request is `<url>?status=<up|down>&msg=<one-line summary>`; the query
of the stored URL is ignored, so the URL can be pasted as Kuma shows it. Any other URL (Healthchecks and so on) is called
exactly as stored. A JSON body goes either way. Kuma keeps only the `msg`, which then shows in the monitor's history.

- `status=up` with a message such as `degraded | disk 93%` is information. `status=down` makes Kuma alert. A sender's
  `degraded_as` setting (default `up`) chooses which a degraded site reports. A **silent** site always alerts.
- A sender sends only when its URL setting starts with `http`. A never-set Home Assistant `input_text` reads `unknown`, which has
  seven characters, so a "length greater than zero" test sends to the text "unknown".

## Secrets and what stays public

This repository is public. Everything here is generic; the table is where each secret lives.

| Secret | Lives | Never |
|---|---|---|
| A monitor's push URL | a root-only file written by `kuma_provision.py`, then in the sender (a hidden HA helper, or `/etc/edge-heartbeat.env`, mode 600) | in git, a PR, a chat or a log; tools print it masked or not at all |
| Kuma admin password | a file on the VM, mode 600 | on a command line |
| Telegram bot token and chat id | files, mode 600 | in git; errors scrub the token |

A push URL is a bearer secret: anyone who has it can make a site look alive. To rotate it, delete the monitor and make it again
(`kuma_provision.py`), then update the sender. Site-specific facts (names of private repositories, real hosts and addresses) stay in
private repositories; examples here use `acme/webapp`, `example.com` and `203.0.113.x`.

## What you see when something fails

All of these look the same from outside: the heartbeats stop, and 15 minutes later a Down message arrives.

| Failure | Symptom at the monitor | What else you may notice |
|---|---|---|
| Site loses power, the Pi crashes, the router or the uplink fails, Home Assistant stops | Down after 15 minutes; Up again within 5 minutes of recovery | nothing from the site itself: it cannot send |
| The site keeps running but is cut off from the internet (a cable unplugged) | the same Down message | the site's own push notifications cannot be delivered either, so only the monitor's alert reaches you; after it reconnects you may see a "reachable again" message with no earlier "unreachable" one |
| The VM or Kuma dies | the watchdog service alerts you | every monitor is silent at once |
| A short outage or a restart (under 15 minutes) | nothing | a gap in the history |

The message does not say **why**. The VM can join your VPN and probe each site directly (ping the router, the Pi), so that "the Pi
answers but Home Assistant is silent" can be told from "nothing answers". That is not built.

## Add a site

1. On the VM, add a monitor for it: `kuma-provision.sh ... --monitor "<Site> heartbeat" --urls-out FILE` (run with `--plan` first).
2. Give the printed-to-file URL to the sender: `ops/heartbeat/install.sh --url-file FILE` for a host, or the helper in the HA UI.
3. **Test the whole chain** (below). A site is not covered until you have seen its Down message arrive.

## The test that matters

Stop the sender, or unplug the network cable of a site that does not need it for 20 minutes, and wait out the window: the Down
message must arrive on your phone, and the Up message must follow after you restore it. Do it once per site and again after any
change to the monitor, the sender or the alert path. A test with Home Assistant's own push notifications proves nothing about the
monitor, and a normal-priority Android push can be held back for a sleeping phone: Home Assistant's alerts should use
`priority: high`, `ttl: 0` (Kuma's Telegram messages did not show this problem). For a small node, load changes with
`ha_node.py sync --reload`: a configuration check on a Raspberry Pi 3 has hung one for minutes and rebooted it.

## Not built, or known limits

- Cause discrimination through the VPN (above).
- Home Assistant's own "unreachable controller" style alerts are armed by a state change, so a restart while the thing is already down
  sends no new alert; a re-check at startup would close it. The outside heartbeat still reports the site.
- One VM is a single point of failure; the watchdog only turns its death into an alert.
- Kuma has no setup API. `kuma_provision.py` drives the Socket.IO channel its web page uses, which a Kuma upgrade could change;
  its logic is tested against a fake and, read-only, against a live Kuma.
- Kuma 1 accepts only GET on a push URL; this design needs Kuma 2 (any method).
