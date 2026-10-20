"""Fetch subprocess cleanup is exercised with real local children and no network."""
import asyncio
import json
import os
import sys
import time

import pytest
from fastapi.testclient import TestClient

from pulsefeed import rss
from pulsefeed.errors import AppError
from pulsefeed.main import create_app

URL = 'https://example.com/rss'


@pytest.fixture
def children(monkeypatch):
    processes = []
    original = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(rss.asyncio, 'create_subprocess_exec', spawn)
    return processes


def command(monkeypatch, code):
    monkeypatch.setattr(rss, '_fetch_command', lambda: (sys.executable, '-c', code))


async def started(children, count):
    for _ in range(200):
        if len(children) >= count:
            return
        await asyncio.sleep(.01)
    pytest.fail('Local fetch child did not start')


def assert_reaped(children):
    assert children
    for child in children:
        assert child.returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(child.pid, 0)


async def test_real_child_success_uses_stdin_and_normalizes_in_child(monkeypatch, children):
    code = ('from pulsefeed import rss; '
            'rss.fetch_bytes=lambda url:(b"<rss><channel><item><title>Story</title><description>Useful description</description></item></channel></rss>",url); '
            'rss._fetch_child()')
    command(monkeypatch, code)
    feed = await rss.fetch_feed(URL)
    assert feed['items'][0]['article'] == {'title': 'Story', 'excerpt': 'Useful description'}
    assert_reaped(children)


async def test_child_failures_transport_only_known_codes(monkeypatch, children):
    command(monkeypatch, 'from pulsefeed import rss; rss.fetch_bytes=lambda url:1/0; rss._fetch_child()')
    with pytest.raises(AppError) as error:
        await rss.fetch_feed(URL)
    assert error.value.code == 'FEED_UNREACHABLE'
    assert 'division' not in error.value.message
    assert_reaped(children)


async def test_trickling_child_has_wall_deadline_and_is_reaped(monkeypatch, children):
    command(monkeypatch, 'import sys,time;\nwhile True:\n sys.stdout.write(" ");sys.stdout.flush();time.sleep(.01)')
    monkeypatch.setattr(rss, 'FETCH_DEADLINE_SECONDS', .15)
    before = time.monotonic()
    with pytest.raises(AppError) as error:
        await rss.fetch_feed(URL)
    assert error.value.code == 'FEED_TIMEOUT'
    assert time.monotonic() - before < 2
    assert_reaped(children)


async def test_blocking_dns_in_real_fetch_child_is_killed_at_wall_deadline(monkeypatch, children):
    command(monkeypatch, 'import time;from pulsefeed import rss;rss.socket.getaddrinfo=lambda *args,**kwargs:time.sleep(60);rss._fetch_child()')
    monkeypatch.setattr(rss, 'FETCH_DEADLINE_SECONDS', .2)
    with pytest.raises(AppError) as error:
        await rss.fetch_feed(URL)
    assert error.value.code == 'FEED_TIMEOUT'
    assert_reaped(children)


async def test_cancel_kills_real_child_even_when_termination_is_ignored(monkeypatch, children):
    command(monkeypatch, 'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print("ready",flush=True);time.sleep(60)')
    task = asyncio.create_task(rss.fetch_feed(URL))
    await started(children, 1)
    # Allow its tiny interpreter to install the signal handler before cancellation.
    await asyncio.sleep(.1)
    before = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - before < 2
    assert_reaped(children)
    # Proves cancellation returned its admission slot after reaping.
    acquired = [rss._FETCH_SLOTS.acquire(blocking=False) for _ in range(4)]
    try:
        assert acquired == [True] * 4
        assert not rss._FETCH_SLOTS.acquire(blocking=False)
    finally:
        for held in acquired:
            if held:
                rss._FETCH_SLOTS.release()


async def test_admission_rejects_fifth_fetch_without_spawning_or_queueing(monkeypatch, children):
    command(monkeypatch, 'import time;time.sleep(60)')
    tasks = [asyncio.create_task(rss.fetch_feed(URL)) for _ in range(4)]
    try:
        await started(children, 4)
        with pytest.raises(AppError) as error:
            await rss.fetch_feed(URL)
        assert (error.value.code, error.value.status) == ('FEED_BUSY', 429)
        assert len(children) == 4
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert_reaped(children)


async def test_oversized_child_output_is_bounded_and_child_reaped(monkeypatch, children):
    command(monkeypatch, 'import sys,time;sys.stdout.buffer.write(b"x"*10000000);sys.stdout.flush();time.sleep(60)')
    with pytest.raises(AppError) as error:
        await rss.fetch_feed(URL)
    assert error.value.code == 'INVALID_FEED'
    assert_reaped(children)


def test_production_command_keeps_feed_url_out_of_arguments():
    assert rss._fetch_command() == (sys.executable, '-m', 'pulsefeed.rss', '--fetch')


async def test_invalid_unicode_url_is_rejected_before_spawning(children):
    with pytest.raises(AppError) as error:
        await rss.fetch_feed('https://example.com/\ud800')
    assert (error.value.code, error.value.status) == ('INVALID_FEED', 400)
    assert children == []


def test_invalid_unicode_url_api_returns_structured_rejection(env, children):
    # httpx's json encoder rejects surrogate input itself; raw JSON exercises the
    # server boundary that a client sending escaped JSON can actually reach.
    body = json.dumps({'url': 'https://example.com/\ud800', 'idempotency_key': 'surrogate-feed-url'})
    app = create_app(env['config'], env['provider'])  # Real production RSS fetcher.
    with TestClient(app) as client:
        client.cookies.update(env['client'].cookies)
        response = client.post('/api/view-feed', content=body,
                               headers=dict(env['headers'], **{'Content-Type': 'application/json'}))
    assert response.status_code == 400
    assert response.json()['error']['code'] == 'INVALID_FEED'
    assert children == []


async def test_shutdown_canceling_cleanup_and_drain_together_reaps_and_releases_slot():
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-c',
        'import signal,sys,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);'
        'sys.stdout.buffer.write(b"x"*524288);sys.stdout.flush();time.sleep(60)',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    await asyncio.sleep(.1)  # Fill the pipe before its cleanup drain starts.
    assert rss._FETCH_SLOTS.acquire(blocking=False)

    async def spawned():
        return process

    spawn = asyncio.create_task(spawned())
    await spawn
    cleanup = asyncio.create_task(rss._reap_fetch(spawn))
    await asyncio.sleep(0)  # Creates the drain but does not run it yet.
    drains = [task for task in asyncio.all_tasks()
              if 'discard_output' in task.get_coro().__qualname__]
    assert len(drains) == 1
    cleanup.cancel()
    for drain in drains:
        drain.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(cleanup), timeout=2)
    assert_reaped([process])
    acquired = [rss._FETCH_SLOTS.acquire(blocking=False) for _ in range(4)]
    try:
        assert acquired == [True] * 4
    finally:
        for held in acquired:
            if held:
                rss._FETCH_SLOTS.release()
