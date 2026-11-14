# Autonomy node (Home Assistant add-on)

The bot of a Home Assistant OS node, packaged as an add-on. HAOS cannot run our deploy scripts, so this is the `ha-addon` packaging described in `docs/design/edge-system.md`: the same bot as on an edge server, with its own Telegram bot and its own owner TOTP secret, and a helper that does /restart, /logs and /reboot through the Supervisor instead of systemd.

## Options

| Option | What |
|---|---|
| `telegram_bot_token` | A bot made for this node with @BotFather (not the edge server's bot) |
| `allowed_ids` | Your numeric Telegram user id (send /id to the bot) |
| `owner_totp_secret` | From `python3 telegram-bot/bot.py --totp-setup`; one authenticator entry per node. Empty leaves /reboot, /restart and peer commands off |
| `node` | This node's name in the fleet file, e.g. `pi-1` (empty = runs alone) |
| `restart_addons` | Add-on slugs `/restart` and `/logs` may name besides `core`, e.g. `core_mosquitto` |
| `peer_grants` | What fleet peers may ask of this node, e.g. `edge-1:restart=core,reboot`; empty = nothing |
| `fleet_file` | The fleet file, default `/share/autonomy/fleet.yaml` (the add-on reads `/share`) |

On first start the add-on creates the node's signing key and prints its public key in the log; add it to the fleet file as this node's `public_key`.

## Permissions

`hassio_api: true` with `hassio_role: manager`, and `homeassistant_api: true`. If the Supervisor answers 403 to a reboot or a restart, change `hassio_role` to `admin` in `config.yaml` and report it: the role needed for `/host/reboot` was not confirmed on a real node when this was written.

## Notes

- Add-on options are visible to the Supervisor and can end up in backups: encrypt your Home Assistant backups.
- There is no `/redeploy`: Home Assistant updates the add-on.
- No Frigate and no camera checks (`FRIGATE_URL` is empty); /status shows Home Assistant, the host and the Supervisor-restartable names.
