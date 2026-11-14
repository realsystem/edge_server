# The edge server as a jumpbox for a Home Assistant OS node

**Status:** Draft · 2026-11-13 · decision: autonomy [ADR-018](https://github.com/realsystem/autonomy/blob/main/design/adr/ADR-018-independent-nodes-peer-mesh-and-owner-totp.md)

## Scope

HAOS runs on the Pi from a flash card or USB; our scripts do not install it. The edge server configures it over the LAN: packages, automations, integrations, add-ons, and the `autonomy-node` add-on. `INSTALL_HOMEASSISTANT` stays for standalone edges that run HA in a container.

## How the jumpbox reaches HAOS

| Channel | Used for | What the owner provides |
|---|---|---|
| SSH to `/config` | copying packages, automations, custom components; `ha` commands | the "Advanced SSH & Web Terminal" add-on with key login (the wizard checks and explains) |
| HA API, long-lived token | `check_config`, core restart, reading states, config flows | a token for a dedicated HA user |
| Supervisor (through the SSH session) | backups, installing and starting add-ons | nothing extra |

## The wizard

Run by `deploy-edge-server.sh` ("manage a Home Assistant node") and also directly as `tools/ha-node.sh`. It asks, and saves the answers to a per-site file (`ha-node.yaml`, no secrets in it), so a rerun asks nothing:
1. the node's address and how to reach it (SSH user, key path, token file);
2. what to deploy, each item opt-in: config packages from a git repo (any repo with the layout `homeassistant/packages/*.yaml`, such as farm_automation), integrations, add-ons, the `autonomy-node` add-on (bot token, TOTP secret, peers);
3. a summary of changes, then confirmation.

Secrets go through the existing `secrets.sh` (SOPS/age); the wizard never prints them.

## Safety rules

- **Backup first** (`ha backups new`); the backup id is recorded.
- **Only files we own.** A manifest on the node lists what we put there (for example `packages/farm_*.yaml`). Anything else is never overwritten or deleted. The one edit to `configuration.yaml` is the `packages:` include, and only after asking.
- **Check, then restart.** `check_config` runs on the new files; on failure the previous files are restored and nothing restarts.
- **Idempotent.** The same answers give no change on a second run.
- **Dry run.** `--plan` prints the changes without making them.

## What stays manual

Steps that need a browser login or a device-side setting (the GW3000's custom server address for the Ecowitt integration, for example). The wizard stops and shows the exact steps. Integrations that only need a config flow (MQTT, say) are done through the API.

## Order of work

1. Connection check and backup (read-only, no risk).
2. Package sync with manifest, `check_config`, rollback; tests with a fake HA node (SSH and API stand-ins).
3. Integrations and add-ons.
4. The `autonomy-node` add-on (bot + Field Station) and its install step; peer setup (key exchange) per `autonomy/design/node-mesh.md`.
