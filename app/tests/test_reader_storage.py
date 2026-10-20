"""Reader payload storage stays bounded independently of inference admission."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json

import pytest

from pulsefeed.config import Config
from pulsefeed.errors import AppError
from test_jobs import finish
from test_view_feed import source


def test_injected_fetcher_cannot_persist_unbounded_or_unknown_fields(env):
    fetched = source(tuple(str(index) for index in range(13)))
    fetched['title'] = 'F' * 100000
    for item in fetched['items']:
        item.update(title='T' * 100000, excerpt='🛰' * 100000,
                    link='javascript:alert(1)', published='P' * 100000,
                    unknown='must not persist')
        item['article'] = {'title':'A' * 100000,'excerpt':'E' * 100000,'unknown':'must not persist'}
    service = env['app'].state.service
    result = service.view_feed(env['owner'],'demo','bounded-fetcher-key',fetched)
    assert len(result['items']) == len(result['runs']) == 10
    assert len(result['feed']['title']) == 200
    for item in result['items']:
        assert len(item['title']) == 500 and len(item['excerpt']) == 4000
        assert len(item['published']) == 200 and item['link'] == ''
        assert 'unknown' not in item
    for row in env['db'].all('SELECT * FROM items'):
        article = json.loads(row['article'])
        assert set(article) == {'title','excerpt'}
        assert len(article['title']) + len(article['excerpt']) == 4000
        assert len(row['title']) == 500 and len(row['excerpt']) == 4000
    snapshot = env['db'].one('SELECT items_snapshot FROM feed_views')['items_snapshot']
    assert len(snapshot.encode()) < 200000 and 'must not persist' not in snapshot
    assert max(len(json.loads(row['original'])['article']['title']) for row in env['db'].all('SELECT original FROM runs')) == 4000
    assert not env['provider'].submissions


@pytest.mark.parametrize('mutate',[
    lambda fetched: fetched.update(items='invalid'),
    lambda fetched: fetched['items'][0].update(article=[]),
    lambda fetched: fetched['items'][0].update(guid='x'*2049),
    lambda fetched: fetched['items'][0].update(title='\ud800'),
])
def test_invalid_injected_payload_does_not_partially_persist(env,mutate):
    fetched = source(('one',))
    mutate(fetched)
    with pytest.raises(AppError) as error:
        env['app'].state.service.view_feed(env['owner'],'demo','invalid-fetcher-key',fetched)
    assert error.value.code == 'INVALID_FEED'
    for table in ('feeds','items','feed_views','runs','batches'):
        assert not env['db'].all('SELECT * FROM '+table)


def test_view_cap_counts_blocked_pages_and_replay_is_free(env):
    env['config'].live_view_limit = 2
    service = env['app'].state.service
    first = service.view_feed(env['owner'],'demo','storage-view-one',source(('one',)))
    blocked = service.view_feed(env['owner'],'demo','storage-view-two',source(('two',)))
    assert blocked['generation']['status'] == 'blocked'
    before = {table:env['db'].all('SELECT * FROM '+table) for table in ('feeds','items','feed_views','runs')}
    with pytest.raises(AppError) as error:
        service.view_feed(env['owner'],'https://example.com/new','storage-view-three',source(('three',)))
    assert error.value.code == 'VIEW_LIMIT_REACHED' and error.value.status == 429
    assert 1 <= error.value.retry_after <= 3601
    assert before == {table:env['db'].all('SELECT * FROM '+table) for table in before}
    # Replay does not inspect a replacement fetch payload or consume capacity.
    replay = service.view_feed(env['owner'],'demo','storage-view-one',{})
    assert replay['view'] == first['view'] and replay['items'] == first['items']
    other = service.view_feed(env['other'],'demo','storage-other-owner',source(('other',)))
    assert other['view']['id'] != first['view']['id']


def test_view_cap_rejects_before_fetch_and_returns_retry_after(env):
    env['config'].live_view_limit = 1
    service = env['app'].state.service
    first = service.view_feed(env['owner'],'demo','prefetch-cap-first',source(('one',)))
    # An invalid network target would fail during fetching; capacity must reject
    # it before reaching that work, while an exact key replay remains available.
    response = env['client'].post('/api/view-feed',json={'url':'https://example.invalid/feed','idempotency_key':'prefetch-cap-second'},headers=env['headers'])
    assert response.status_code == 429 and response.headers['retry-after']
    assert response.json()['error']['code'] == 'VIEW_LIMIT_REACHED'
    replay = env['client'].post('/api/view-feed',json={'url':'demo','idempotency_key':'prefetch-cap-first'},headers=env['headers'])
    assert replay.status_code == 200 and replay.json()['view'] == first['view']
    assert len(env['db'].all('SELECT * FROM feed_views')) == 1


def test_concurrent_fresh_keys_cannot_overrun_live_view_cap(env):
    env['config'].live_view_limit = 1
    service = env['app'].state.service
    def create(index):
        try:
            return service.view_feed(env['owner'],'demo','concurrent-storage-'+str(index),source(('one',)))
        except AppError as error:
            return error
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(create,range(3)))
    assert sum(isinstance(result,dict) for result in results) == 1
    assert [result.code for result in results if isinstance(result,AppError)] == ['VIEW_LIMIT_REACHED']*2
    assert len(env['db'].all('SELECT * FROM feed_views')) == len(env['db'].all('SELECT * FROM runs')) == 1


async def test_expired_compaction_preserves_tombstones_model_history_and_live_pages(env):
    service = env['app'].state.service
    first = service.view_feed(env['owner'],'demo','compact-expired-key',source(('one',)))
    await finish(env)
    live = service.view_feed(env['owner'],'demo','compact-live-key',source(('two',)))
    env['db'].execute('UPDATE feed_views SET expires_at=0 WHERE id=?',(first['view']['id'],))
    old = env['db'].one('SELECT * FROM feed_views WHERE id=?',(first['view']['id'],))
    before = {table:env['db'].all('SELECT * FROM '+table) for table in ('feeds','items','runs','invocations','batches','view_runs')}
    charges = service.quota_summary()['global']['charged']
    assert env['db'].compact_expired_views(limit=1) == 1
    assert env['db'].compact_expired_views(limit=1) == 0
    compacted = env['db'].one('SELECT * FROM feed_views WHERE id=?',(first['view']['id'],))
    assert compacted == dict(old,feed_snapshot='{}',items_snapshot='[]')
    assert before == {table:env['db'].all('SELECT * FROM '+table) for table in before}
    assert service.quota_summary()['global']['charged'] == charges
    assert service.reader_view(env['owner'],live['view']['id'])['items'] == live['items']
    for replay in (lambda:service.cached_view(env['owner'],'compact-expired-key','demo'),
                   lambda:service.view_feed(env['owner'],'demo','compact-expired-key',source(('new',)))):
        with pytest.raises(AppError) as error:
            replay()
        assert error.value.code == 'VIEW_NOT_FOUND'
    assert len(env['db'].all('SELECT * FROM runs')) == 2


async def test_tick_compacts_inactive_reader_without_releasing_unknown_work(env):
    service = env['app'].state.service
    first = service.view_feed(env['owner'],'demo','compact-unknown-key',source(('one',)))
    env['provider'].behavior['qwen'] = 'unknown'
    await env['app'].state.coordinator.tick()
    run_before = env['db'].one('SELECT * FROM runs')
    assert run_before['state'] == 'submission_unknown'
    env['db'].execute('UPDATE feed_views SET expires_at=0 WHERE id=?',(first['view']['id'],))
    await env['app'].state.coordinator.tick()
    assert env['db'].one('SELECT items_snapshot FROM feed_views')['items_snapshot'] == '[]'
    assert env['db'].one('SELECT * FROM runs') == run_before
    assert service.quota_summary()['global']['reserved'] == 2


def test_expiry_frees_view_capacity_without_reusing_expired_key(env,monkeypatch):
    env['config'].live_view_limit = 1
    service = env['app'].state.service
    first = service.view_feed(env['owner'],'demo','expiry-cap-first',source(('one',)))
    monkeypatch.setattr('pulsefeed.service.time.time',lambda:first['view']['expires_at'])
    fresh = service.view_feed(env['owner'],'demo','expiry-cap-second',source(('two',)))
    assert fresh['view']['id'] != first['view']['id']
    assert env['db'].one('SELECT items_snapshot FROM feed_views WHERE id=?',(first['view']['id'],))['items_snapshot'] == '[]'
    with pytest.raises(AppError) as error:
        service.cached_view(env['owner'],'expiry-cap-first','demo')
    assert error.value.status == 404


def test_live_view_config_bounds_and_legacy_environment_fallback(env,monkeypatch):
    monkeypatch.setenv('SESSION_SECRET','x'*40)
    monkeypatch.setenv('PULSEFEED_FAKE_MODE','true')
    monkeypatch.delenv('LIVE_VIEW_LIMIT',raising=False)
    monkeypatch.setenv('SAVED_FEED_LIMIT','7')
    assert Config.from_env().live_view_limit == 7
    monkeypatch.setenv('LIVE_VIEW_LIMIT','9')
    assert Config.from_env().live_view_limit == 9
    for limit in (0,-1,True,101,1.5):
        with pytest.raises(ValueError):
            replace(env['config'],live_view_limit=limit).validate()


def test_invalid_json_depth_or_surrogate_key_returns_structured_error(env):
    deep = '{"url":'+ '['*2000 + '0' + ']'*2000 + ',"idempotency_key":"deep-json-key"}'
    for body in (deep,json.dumps({'url':'demo','idempotency_key':'surrogate-key-\ud800'})):
        response = env['client'].post('/api/view-feed',content=body,headers=env['headers'])
        assert response.status_code == 400 and response.json()['error']['code'] == 'INVALID_INPUT'
    assert not env['db'].all('SELECT * FROM feed_views')


def test_expired_payload_compaction_is_bounded_per_transaction(env):
    service = env['app'].state.service
    for index in range(3):
        service.view_feed(env['owner'],'demo','compaction-batch-'+str(index),source(('one',)))
    env['db'].execute('UPDATE feed_views SET expires_at=0')
    assert env['db'].compact_expired_views(limit=1) == 1
    assert env['db'].one("SELECT COUNT(*) AS n FROM feed_views WHERE items_snapshot!='[]'")['n'] == 2
    assert env['db'].compact_expired_views(limit=1) == 1
    assert env['db'].compact_expired_views(limit=1) == 1
    assert env['db'].compact_expired_views(limit=1) == 0
    assert len(env['db'].all('SELECT * FROM feed_views')) == 3
