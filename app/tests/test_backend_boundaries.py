"""Transport deadlines and database work remain bounded on public read paths."""
import asyncio
import math

import httpx
import pytest

from conftest import start
from pulsefeed import coordinator as coordinator_module
from pulsefeed.errors import AppError
from pulsefeed.provider import MAX_READ_RETRY_DELAY, RunpodAdapter


@pytest.mark.parametrize('hint', ['inf', 'NaN', '999999999999999999', 'Wed, 31 Dec 9999 23:59:59 GMT', '-9', 'invalid'])
async def test_retry_hints_are_finite_and_bounded(env, hint):
    delays = []
    calls = []
    async def transport(request):
        calls.append(request)
        return httpx.Response(429, headers={'Retry-After': hint}) if len(calls) == 1 else httpx.Response(200, json={'status': 'IN_PROGRESS'})
    async def sleep(delay):
        delays.append(delay)
    adapter = RunpodAdapter(env['config'], client=httpx.AsyncClient(transport=httpx.MockTransport(transport)), sleep=sleep)
    try:
        assert await adapter.status('flux', 'job') == {'status': 'IN_PROGRESS'}
        assert len(delays) == 1
        assert math.isfinite(delays[0]) and 0 <= delays[0] <= MAX_READ_RETRY_DELAY
    finally:
        await adapter.close()


async def completed_run(env):
    batch = start(env)
    coordinator = env['app'].state.coordinator
    for _ in range(5):
        await coordinator.tick()
    run = env['db'].one('SELECT * FROM runs WHERE batch_id=?', (batch['id'],))
    assert run['state'] == 'completed'
    return coordinator, run


async def test_image_retrieval_deadline_does_not_change_accounting(env, monkeypatch):
    coordinator, run = await completed_run(env)
    before = env['db'].all('SELECT * FROM runs')
    cancelled = asyncio.Event()
    async def blocked(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr(env['provider'], 'status', blocked)
    monkeypatch.setattr(coordinator_module, 'IMAGE_RETRIEVAL_TIMEOUT', 0.01)
    with pytest.raises(AppError) as error:
        await coordinator.retrieve(env['owner'], run['id'])
    assert error.value.code == 'RESULT_UNAVAILABLE'
    assert cancelled.is_set()
    assert env['db'].all('SELECT * FROM runs') == before


async def test_image_retrieval_preserves_caller_cancellation(env, monkeypatch):
    coordinator, run = await completed_run(env)
    entered = asyncio.Event()
    async def blocked(*args):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(env['provider'], 'status', blocked)
    task = asyncio.create_task(coordinator.retrieve(env['owner'], run['id']))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_expired_rate_cleanup_uses_created_index(env):
    plan = env['db'].all('EXPLAIN QUERY PLAN DELETE FROM rate_events WHERE created<?', (1,))
    assert any('rate_expiry' in row['detail'] for row in plan)


def test_poll_limit_precedes_batch_serialization(env, monkeypatch):
    service = env['app'].state.service
    def limited(*args):
        raise AppError('RATE_LIMITED', 'Too many requests.', 429)
    def unexpected(*args):
        pytest.fail('A rate-limited request must not serialize batch history')
    monkeypatch.setattr(service, 'limit', limited)
    monkeypatch.setattr(service, 'view_batch', unexpected)
    response = env['client'].get('/api/batches/missing?view_id=missing')
    assert response.status_code == 429
    assert response.json()['error']['code'] == 'RATE_LIMITED'
