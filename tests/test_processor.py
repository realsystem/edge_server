# ruff: noqa: F811, F401, E402 - pytest fixtures are imported from test_telegram_bot and used as arguments
"""The job processor control plane on this node (INSTALL_PROCESSOR=true): /status shows its
queue and online workers, and the watcher alerts when jobs wait with no worker."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_telegram_bot import (
    T0,
    TOKEN,
    FakeServer,
    bot,
    frigate,
    host,
    make_bot,
    run_checks,
    telegram,
    watcher,
)

KEY = "pk_0123456789abcdef_" + "a" * 40


def stats(queued=0, oldest=0, workers=(), **more):
    jobs = {"queued": queued, **more}
    return {
        "success": True,
        "data": {
            "jobs": jobs,
            "oldest_queued_s": oldest,
            "workers_online": [
                {"id": w, "capabilities": ["llm.chat", "ollama_chat"]} for w in workers
            ],
        },
    }


@pytest.fixture
def processor():
    s = FakeServer({"/api/v1/stats": (200, stats())})
    yield s
    s.close()


def with_processor(b, processor, **more):
    b.cfg = b.cfg.__class__(
        **{**b.cfg.__dict__, "processor_url": processor.url, "processor_api_key": KEY, **more}
    )
    return b


def test_config_from_env():
    cfg = bot.Config.from_env(
        {
            "TELEGRAM_BOT_TOKEN": TOKEN,
            "TELEGRAM_ALLOWED_IDS": "1",
            "PROCESSOR_URL": "http://processor:8080/",
            "PROCESSOR_API_KEY": KEY,
            "PROCESSOR_ALERT_MINUTES": "45",
        }
    )
    assert (cfg.processor_url, cfg.processor_api_key, cfg.processor_alert_minutes) == (
        "http://processor:8080",
        KEY,
        45.0,
    )
    with pytest.raises(ValueError, match="PROCESSOR_ALERT_MINUTES"):
        bot.Config.from_env(
            {
                "TELEGRAM_BOT_TOKEN": TOKEN,
                "TELEGRAM_ALLOWED_IDS": "1",
                "PROCESSOR_ALERT_MINUTES": "x",
            }
        )


def test_status_shows_queue_and_workers(telegram, frigate, host, processor):
    processor.routes["/api/v1/stats"] = (
        200,
        stats(
            queued=2, oldest=95, workers=["gpu-1"], running=1, succeeded=40, failed=1, dead_letter=1
        ),
    )
    b = with_processor(make_bot(telegram, frigate, host), processor)
    text = bot.build_status(b.cfg, lambda s: None)
    assert "processor: 2 queued (oldest 1m), 1 running, 40 done, 2 failed" in text
    assert "processor workers online: gpu-1 (llm.chat, ollama_chat)" in text
    assert "problem" not in text.splitlines()[0]


def test_status_without_processor_has_no_line(telegram, frigate, host):
    assert "processor" not in bot.build_status(
        make_bot(telegram, frigate, host).cfg, lambda s: None
    )


def test_unreachable_or_refused_is_a_problem(telegram, frigate, host, processor):
    b = with_processor(make_bot(telegram, frigate, host), processor)
    processor.routes["/api/v1/stats"] = (401, {"success": False})
    text = bot.build_status(b.cfg, lambda s: None)
    assert "processor: HTTP 401 (check PROCESSOR_API_KEY)" in text
    assert "1 problem(s)" in text.splitlines()[0]
    b.cfg = b.cfg.__class__(**{**b.cfg.__dict__, "processor_url": "http://127.0.0.1:9"})
    assert "processor: no answer" in bot.build_status(b.cfg, lambda s: None)


def test_jobs_waiting_without_a_worker(telegram, frigate, host, processor):
    b = with_processor(make_bot(telegram, frigate, host), processor)
    processor.routes["/api/v1/stats"] = (200, stats(queued=3, oldest=10 * 60))
    assert "problem" not in bot.build_status(b.cfg, lambda s: None).splitlines()[0]  # not yet
    processor.routes["/api/v1/stats"] = (200, stats(queued=3, oldest=31 * 60))
    text = bot.build_status(b.cfg, lambda s: None)
    assert "3 job(s) have waited 31m and no processor worker is online" in text
    processor.routes["/api/v1/stats"] = (200, stats(queued=3, oldest=31 * 60, workers=["gpu-1"]))
    assert (
        "problem" not in bot.build_status(b.cfg, lambda s: None).splitlines()[0]
    )  # busy, not stuck


def test_watcher_alerts_and_recovers(watcher, processor):
    b = with_processor(watcher, processor)
    processor.routes["/api/v1/stats"] = (200, stats(queued=1, oldest=3600))
    first, second = run_checks(b, 2)
    assert first == []  # confirmed on the second check
    assert any("no processor worker is online" in m for m in second)
    processor.routes["/api/v1/stats"] = (200, stats(queued=0, workers=["gpu-1"]))
    (third,) = run_checks(b, 1, start=T0 + 900)
    assert "✅ Job processor: back to normal." in third


def test_key_from_a_file_read_at_each_check(telegram, frigate, host, processor, tmp_path):
    keyfile = tmp_path / "processor.key"
    b = with_processor(
        make_bot(telegram, frigate, host),
        processor,
        processor_api_key="",
        processor_api_key_file=keyfile,
    )
    assert "processor: no API key yet" in bot.build_status(b.cfg, lambda s: None)
    keyfile.write_text(KEY + "\n")  # the deploy script fills it in after the bot started
    assert "processor: 0 queued" in bot.build_status(b.cfg, lambda s: None)
