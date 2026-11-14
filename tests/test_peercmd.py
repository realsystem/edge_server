# ruff: noqa: F811, F401, E402 - pytest fixtures are imported and used as arguments
"""Commands between fleet nodes (ADR-018): /remote on one node, a checked request on the other.

Two bots share an in-memory fleet (the one from test_controlplane). Node ``a`` sends, node ``b``
receives. The receiver must refuse anything without the owner's code for *its* secret."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "telegram-bot"))
pytest.importorskip("cryptography")
pytest.importorskip("autonomy_core")
import peercmd
from test_controlplane import Clock, Fleet
from test_telegram_bot import (
    ALICE,
    COMPOSE,
    NOW,
    RFC_SECRET,
    TOKEN,
    bot,
    frigate,
    host,
    make_bot,
    message,
    telegram,
)


def code(offset=0):
    return bot.totp(RFC_SECRET, int(NOW // 30) + offset)


@pytest.fixture
def pair(tmp_path, host, telegram, frigate):
    clock = Clock(NOW)
    fleet = Fleet(tmp_path / "fleet", clock)
    (tmp_path / "fleet").mkdir(exist_ok=True)
    requests = host / "requests"
    requests.mkdir()
    (host / "app" / "docker-compose.yml").write_text(COMPOSE)

    def node(name, **kw):
        b = make_bot(telegram, frigate, host, totp_secret=RFC_SECRET, requests_dir=requests, **kw)
        b.clock = clock
        b.control = fleet.planes[name]
        return b

    class Pair:
        pass

    p = Pair()
    p.fleet, p.clock, p.telegram, p.requests = fleet, clock, telegram, requests
    p.a = node("a")
    p.b = node("b", peer_grants="a:restart=frigate,reboot", reboot_min_uptime=60)
    p.b.setup_peer_commands()
    # the fixture's sleep is a no-op: the job-watch thread would give up on the host at once
    # and delete the request under the test
    p.b.sleep = time.sleep
    p.b.cfg.job_poll = 0.01
    return p


def remote(p, text, user=ALICE, chat_type="private", date=NOW):
    before = len(p.telegram.texts())
    p.a.handle_update(message(user, text, date=date, chat_type=chat_type))
    # the receiver tells its owner from another thread into the same fake Telegram, so the
    # sender's own reply is not always the last text: return everything sent during the call
    return " | ".join(p.telegram.texts()[before:])


def say(p):
    time.sleep(0.1)  # the receiver tells its owner from a thread
    return " | ".join(p.telegram.texts())


def test_a_restart_with_the_owners_code_is_sent_checked_and_started(pair):
    reply = remote(pair, f"/remote b restart frigate {code()}")
    assert "Sent to b" in reply
    assert json.loads((pair.requests / "restart-request").read_text()) == {"service": "frigate"}
    assert (pair.requests / "totp-last-step").read_text() == str(int(NOW // 30))
    assert "a asked to restart frigate" in say(pair)
    assert pair.telegram.sent("deleteMessage")  # the sender removed the code message


def test_a_reboot_with_the_code_requests_the_reboot(pair):
    assert "Sent to b" in remote(pair, f"/remote b reboot {code()}")
    assert (pair.requests / "reboot").exists()
    assert json.loads((pair.requests / "reboot-notify").read_text())["chat_id"] == ALICE


def test_a_wrong_code_is_refused_and_nothing_runs(pair):
    reply = remote(pair, "/remote b restart frigate 000000")
    assert "Not delivered" in reply
    assert not (pair.requests / "restart-request").exists()
    assert "Refused a request from a: restart frigate (bad code)" in say(pair)


def test_a_used_code_cannot_be_used_twice(pair):
    assert "Sent to b" in remote(pair, f"/remote b restart frigate {code()}")
    (pair.requests / "restart-request").unlink()
    pair.clock.now += 11  # past the sender's own pause; still the same 30 s window
    assert "Not delivered" in remote(
        pair, f"/remote b restart frigate {code()}", date=pair.clock.now
    )
    assert not (pair.requests / "restart-request").exists()


def test_a_service_outside_the_grant_is_refused_and_does_not_burn_the_code(pair):
    reply = remote(pair, f"/remote b restart mosquitto {code()}")
    assert "Not delivered" in reply
    assert not (pair.requests / "totp-last-step").exists()
    assert "not granted" in say(pair)


def test_without_peer_grants_the_node_accepts_nothing(pair):
    pair.b.control.on_command = None  # what a node without PEER_GRANTS looks like
    assert "Not delivered" in remote(pair, f"/remote b restart frigate {code()}")
    assert not (pair.requests / "restart-request").exists()


def test_a_peer_that_is_not_granted_anything_is_refused(pair):
    pair.b.cfg = bot.Config(**{**pair.b.cfg.__dict__, "peer_grants": "c:reboot"})
    pair.b.setup_peer_commands()
    assert "Not delivered" in remote(pair, f"/remote b restart frigate {code()}")
    assert not (pair.requests / "restart-request").exists()


def test_a_reboot_is_refused_while_the_uptime_is_short(pair, host):
    (host / "proc" / "uptime").write_text("5.0 1.0\n")
    assert "Not delivered" in remote(pair, f"/remote b reboot {code()}")
    assert not (pair.requests / "reboot").exists()


def test_remote_needs_a_private_chat_a_known_node_and_well_formed_arguments(pair):
    assert "private chat" in remote(pair, f"/remote b reboot {code()}", chat_type="group")
    assert "Unknown node" in remote(pair, f"/remote zz reboot {code()}")
    assert "Usage" in remote(pair, "/remote b restart")
    assert "Usage" in remote(pair, f"/remote b restart frigate extra {code()}")
    assert "6 digits" in remote(pair, "/remote b reboot 12ab56")
    assert "too old" in remote(pair, f"/remote b reboot {code()}", date=NOW - 90)
    assert not (pair.requests / "reboot").exists()


def test_remote_needs_a_pause_between_commands(pair):
    remote(pair, "/remote b restart frigate 000000")
    assert "wait a few seconds" in remote(pair, "/remote b restart frigate 000001")


def test_remote_off_outside_a_fleet(pair):
    pair.a.control = None
    assert "not in a fleet" in remote(pair, f"/remote b reboot {code()}")


# --------------------------------------------------------------------------- config and grants


def test_grants_parse_and_typos_are_errors():
    assert peercmd.parse_grants("pi-1:restart=frigate|mosquitto,reboot; edge:reboot") == {
        "pi-1": (
            frozenset({"restart_service", "reboot_host"}),
            frozenset({"frigate", "mosquitto"}),
        ),
        "edge": (frozenset({"reboot_host"}), frozenset()),
    }
    assert peercmd.parse_grants("") == {}
    for bad in ("pi-1", "pi-1:", "pi-1:shell", "pi-1:restart=", "Pi 1:reboot", "pi-1:restart=a b"):
        with pytest.raises(ValueError):
            peercmd.parse_grants(bad)


def test_config_reads_the_new_secret_name_and_the_old_one():
    env = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_ALLOWED_IDS": "1", "REQUESTS_DIR": "/r"}
    assert bot.Config.from_env({**env, "OWNER_TOTP_SECRET": RFC_SECRET}).totp_secret == RFC_SECRET
    assert bot.Config.from_env({**env, "REBOOT_TOTP_SECRET": RFC_SECRET}).totp_secret == RFC_SECRET
    new = bot.Config.from_env(
        {**env, "OWNER_TOTP_SECRET": RFC_SECRET, "REBOOT_TOTP_SECRET": "x" * 20}
    )
    assert new.totp_secret == RFC_SECRET  # the new name wins
    with pytest.raises(ValueError, match="PEER_GRANTS"):
        bot.Config.from_env({**env, "PEER_GRANTS": "pi-1:shell"})
    assert bot.Config.from_env({**env, "PEER_GRANTS": "pi-1:reboot"}).peer_grants == "pi-1:reboot"
