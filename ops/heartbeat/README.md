# Heartbeat senders

The other half of [`ops/monitor/`](../monitor/README.md). A monitor outside the site raises an alert when heartbeats stop; these
are small, generic senders that make the heartbeats. They know nothing about any particular site: they say "I am alive" and a
few facts about the machine. Anything that belongs to one site (soil sensors, a pump, a controller) belongs in that site's own
repository. A site can use its own richer sender instead of these; use one sender per monitor.

| Sender | For | Files |
|---|---|---|
| **Host sender** | a NAS, the video server, a VM: any systemd Linux | `node-heartbeat.sh`, `install.sh` |
| **Home Assistant package** | any Home Assistant (home, farm, a test bench) | `ha/edge_heartbeat.yaml` |

Both call the monitor the same way. For an Uptime Kuma push URL (it contains `/api/push/`) the request is
`<url>?status=<up|down>&msg=<one-line summary>`, and the query of the stored URL is ignored, so you can paste the URL exactly as
Kuma shows it. Any other URL (Healthchecks and so on) is called exactly as stored. A JSON body is sent either way. The URL is a
**secret**: it lives in a root-only file or a hidden Home Assistant helper, and the tools never print it.

## Host sender

```
sudo ./ops/heartbeat/install.sh --url-file /root/this-hosts-push-url --name nas-1
```

`--url-file` holds one line, the push URL of a monitor made for this host (`ops/monitor/kuma-provision.sh` writes such lines).
Without it the installer asks with the typing hidden, or reads `$HEARTBEAT_URL`; a URL on the command line works but stays in shell
history. It installs `/usr/local/sbin/edge-heartbeat`, `/etc/edge-heartbeat.env` (mode 600) and a systemd timer (every 5 minutes by
default, `--interval-min`), sends one heartbeat at once, and can be run again or removed with `--uninstall`.

The message looks like `ok | up 12 d | disk 41% | mem 62% | load 0.31`. A host whose disk use reaches `--disk-percent` (90) is
"degraded": by default that only shows in the message (`status=up`); with `--degraded-as down` the monitor raises an alert. A host
that stops sending is always an alert. Look at the last beats with `journalctl -u edge-heartbeat -n 20`, try it with
`edge-heartbeat --dry-run` (it hides the push token), and stop it for maintenance with `sudo systemctl stop edge-heartbeat.timer`.

## Home Assistant package

Copy `ha/edge_heartbeat.yaml` into the `packages/` folder of the HA configuration (`tools/ha_node.py sync` does this for a node; add `--reload` on a small node such as a Raspberry Pi 3, so that no configuration check runs and nothing restarts),
set the URL in the HA UI (Settings > Devices & services > Helpers > "Edge heartbeat URL"), and reload `input_text`,
`input_datetime`, `template`, `rest_command` and `automation` (a first-time `rest_command` needs one restart). Nothing is sent until
the helper starts with `http`: an unset helper reads `unknown`, which is why the check is for `http`, not for a length. It
reports `ok | HA up 12.3 h | 3 unavailable`.

## Testing a sender end to end

The whole system is described in [`docs/design/external-heartbeat.md`](../../docs/design/external-heartbeat.md). A sender is not proven until an alert has reached a phone. With the sender running and the monitor up: stop the sender (or unplug
the network cable of the site) and wait for the monitor's window to pass (15 minutes with the default 900-second Kuma monitor).
The "Down" message must arrive on the phone; then restore the sender and the "Up" message must follow. An alarm that has never
been seen to fire is not an alarm.
