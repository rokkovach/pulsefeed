from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import time

import pytest

from pulsefeed.db import Database
from pulsefeed.errors import AppError
from pulsefeed.usage import aggregate_usage
from test_jobs import finish
from test_view_feed import source


def view(env,key='ephemeral-view-1',url='demo',fetched=None,owner=None):
    return env['app'].state.service.view_feed(owner or env['owner'],url,key,fetched or source())


def test_fixed_ttl_replay_and_expired_key_never_recreates(env,monkeypatch):
    clock={'now':time.time()}
    monkeypatch.setattr('pulsefeed.service.time.time',lambda:clock['now'])
    first=view(env)
    assert first['view']['expires_at']==first['view']['created_at']+3600
    assert first['view']['url']=='/feeds/'+first['view']['id']
    clock['now']+=3599
    replay=env['app'].state.service.cached_view(env['owner'],'ephemeral-view-1','demo')
    assert replay['view']==first['view'] and replay['generation']['status']=='reused'
    assert [r['id'] for r in replay['runs']]==[r['id'] for r in first['runs']]
    clock['now']+=1
    with pytest.raises(AppError) as error:
        env['app'].state.service.cached_view(env['owner'],'ephemeral-view-1','demo')
    assert error.value.code=='VIEW_NOT_FOUND' and error.value.status==404
    assert len(env['db'].all('SELECT * FROM runs'))==5
    assert len(env['db'].all('SELECT * FROM feed_views'))==1
    assert not env['provider'].submissions


async def test_new_key_fresh_runs_and_frozen_articles_even_same_internal_feed(env):
    first=view(env)
    await finish(env)
    edited=source()
    edited['items'][0]['title']='Changed in a later fetch'
    edited['items'][0]['article']['title']='Changed in a later fetch'
    second=view(env,'ephemeral-view-2',fetched=edited)
    assert second['view']['id']!=first['view']['id']
    assert second['feed']['id']==first['feed']['id']
    assert {r['id'] for r in first['runs']}.isdisjoint(r['id'] for r in second['runs'])
    restored=env['app'].state.service.reader_view(env['owner'],first['view']['id'])
    assert restored['items']==first['items']
    assert [r['id'] for r in restored['runs']]==[r['id'] for r in first['runs']]
    assert second['items'][0]['title']=='Changed in a later fetch'
    assert not restored['active_batch_id']


def test_concurrent_keys_isolate_started_and_blocked_views(env):
    with ThreadPoolExecutor(max_workers=3) as pool:
        results=list(pool.map(lambda key:view(env,key),['concurrent-view-1','concurrent-view-2','concurrent-view-1']))
    assert len({result['view']['id'] for result in results})==2
    blocked=next(result for result in results if result['generation']['status']=='blocked')
    assert blocked['runs']==[] and blocked['active_batch_id'] is None
    assert len(env['db'].all('SELECT * FROM view_runs'))==5


async def test_expiry_does_not_cancel_reconcile_refund_or_erase_usage(env):
    first=view(env)
    service=env['app'].state.service
    env['db'].execute('UPDATE feed_views SET expires_at=0 WHERE id=?',(first['view']['id'],))
    assert service.quota_summary()['global']['reserved']==10
    await finish(env)
    assert all(run['state']=='completed' for run in service.batch(env['owner'],first['generation']['batch']['id'])['runs'])
    assert service.quota_summary()['global']['used']==5
    assert service.quota_summary()['hourly']['used']==5
    assert sum(row['invocations'] for row in aggregate_usage(env['db'],'all')['models'])==10
    assert not any(job['cancelled'] for job in env['provider'].jobs.values())


async def test_retry_is_bound_to_view_lineage_and_does_not_renew_ttl(env):
    first=view(env)
    await finish(env)
    second=view(env,'another-view-key',fetched=source(('other',)))
    await finish(env)
    run=first['runs'][0]
    service=env['app'].state.service
    with pytest.raises(AppError) as wrong:
        service.retry_view(env['owner'],second['view']['id'],run['id'],'wrong-view-retry')
    assert wrong.value.status==404
    retry=service.retry_view(env['owner'],first['view']['id'],run['id'],'correct-view-retry')
    assert retry['runs'][0]['parent_run_id']==run['id']
    assert service.reader_view(env['owner'],first['view']['id'])['view']==first['view']
    assert service.quota_summary()['hourly']['used']==7
    assert service.retry_view(env['owner'],first['view']['id'],run['id'],'correct-view-retry')['id']==retry['id']
    assert service.quota_summary()['hourly']['used']==7
    assert service.reader_view(env['owner'],second['view']['id'])['runs'][0]['id']==second['runs'][0]['id']


def test_view_gated_apis_no_missing_foreign_or_mismatched_scope(env):
    first=view(env)
    other=view(env,'other-owner-view',owner=env['other'],fetched=source(('other',)))
    batch=first['generation']['batch']['id'];run=first['runs'][0]['id']
    for suffix in ('','?view_id=missing','?view_id='+other['view']['id']):
        for method,path,body in [('GET','/api/batches/'+batch,None),('GET','/api/runs/'+run+'/image',None),('POST','/api/runs/'+run+'/retry',{'idempotency_key':'expired-retry-key'})]:
            response=env['client'].request(method,path+suffix,json=body,headers=env['headers'])
            assert response.status_code==404 and response.json()['error']['code']=='VIEW_NOT_FOUND'
    assert env['client'].get('/api/batches/'+batch+'?view_id='+first['view']['id']).status_code==200
    env['db'].execute('UPDATE feed_views SET expires_at=0 WHERE id=?',(first['view']['id'],))
    for path in ('/api/batches/'+batch,'/api/runs/'+run+'/image'):
        assert env['client'].get(path+'?view_id='+first['view']['id']).status_code==404
    assert not env['provider'].submissions


async def test_image_expiring_during_provider_read_is_not_returned(env,monkeypatch):
    first=view(env)
    async def retrieve(*args):
        env['db'].execute('UPDATE feed_views SET expires_at=0 WHERE id=?',(first['view']['id'],))
        return b'not-permitted-after-expiry','image/webp'
    monkeypatch.setattr(env['app'].state.coordinator,'retrieve',retrieve)
    response=env['client'].get('/api/runs/'+first['runs'][0]['id']+'/image?view_id='+first['view']['id'])
    assert response.status_code==404 and b'not-permitted-after-expiry' not in response.content


@pytest.mark.parametrize(('method','path'),[('POST','/api/feeds'),('GET','/api/feeds/old/items'),('POST','/api/feeds/old/refresh'),('POST','/api/batches'),('GET','/api/runs'),('GET','/fragments/history')])
def test_retired_reader_paths_cannot_bypass_expiry(env,method,path):
    response=env['client'].request(method,path,json={},headers=env['headers'])
    assert response.status_code==404 and response.json()['error']['code']=='VIEW_NOT_FOUND'
    assert not env['provider'].submissions


async def test_global_hourly_starts_count_qwen_failures_until_explicit_reset(env):
    env['db'].execute('UPDATE quota_policy SET global_hourly_generation_limit=3')
    env['provider'].behavior['qwen']='failure'
    first=view(env)
    await finish(env)
    service=env['app'].state.service
    assert service.quota_summary()['global']['charged']==0
    assert service.quota_summary()['hourly']['used']==3
    service.reset_limits(env['admin'],{'confirm':True})
    assert service.quota_summary()['hourly']['used']==0
    fresh=view(env,'hourly-after-reset',url='another-feed')
    assert fresh['generation']['status']=='started'
    assert len(fresh['runs'])==3
    assert service.cached_view(env['owner'],'ephemeral-view-1','demo')['view']==first['view']
    assert service.quota_summary()['hourly']['used']==3


def test_global_hourly_atomic_boundary_across_owners(env):
    env['db'].execute('UPDATE quota_policy SET global_hourly_generation_limit=1')
    def admit(pair):return view(env,pair[0],owner=pair[1],fetched=source(('one',)))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(admit,[('hourly-owner-a',env['owner']),('hourly-owner-b',env['other'])]))
    assert sum(len(result['runs']) for result in results)==1
    run=env['db'].one('SELECT * FROM runs')
    env['app'].state.coordinator.finish(run,reserved=0)
    blocked=view(env,'hourly-owner-c',owner=env['other'],fetched=source(('one',)))
    assert blocked['generation']['error']['code']=='GLOBAL_HOURLY_LIMIT'


def test_hourly_window_boundary_policy_persistence_and_env_validation(env,tmp_path,monkeypatch):
    first=view(env,fetched=source(('one',)))
    created=first['runs'][0]['created']
    service=env['app'].state.service
    assert service.quota_summary(now=created+3599.999)['hourly']['used']==1
    assert service.quota_summary(now=created+3600)['hourly']['used']==0
    assert service.quota_summary(now=created+3599)['hourly']['next_slot_at']==created+3600
    path=str(tmp_path/'hourly.db')
    Database(path)  # CLI can initialize accounts/policy before the app seeds its limit.
    db=Database(path,100,10,12)
    assert db.one('SELECT global_hourly_generation_limit FROM quota_policy')['global_hourly_generation_limit']==12
    Database(path,100,10,999)
    assert db.one('SELECT global_hourly_generation_limit FROM quota_policy')['global_hourly_generation_limit']==12
    for limit in (0,-1,True,10001):
        with pytest.raises(ValueError):replace(env['config'],global_hourly_generation_limit=limit).validate()


def test_legacy_view_migration_is_expired_without_deleting_accounting(env):
    first=view(env)
    before={table:env['db'].all('SELECT * FROM '+table) for table in ('runs','batches','view_runs')}
    # A real pre-v4 database has no identity/expiry columns or membership table.
    with env['db'].transaction() as conn:
        conn.execute('DROP TABLE view_runs')
        conn.execute('DROP INDEX view_identity')
        conn.execute('DROP INDEX view_owner_expiry')
        conn.execute('DROP INDEX view_pending_compaction')
        conn.execute('ALTER TABLE feed_views DROP COLUMN id')
        conn.execute('ALTER TABLE feed_views DROP COLUMN expires_at')
        conn.execute('PRAGMA user_version=3')
    Database(env['config'].database_path,100,10,999)
    row=env['db'].one('SELECT id,expires_at FROM feed_views')
    assert row['id'] and row['expires_at']==0
    with pytest.raises(AppError) as error:
        env['app'].state.service.cached_view(env['owner'],'ephemeral-view-1','demo')
    assert error.value.status==404
    assert before['runs']==env['db'].all('SELECT * FROM runs')
    assert before['batches']==env['db'].all('SELECT * FROM batches')
    assert env['app'].state.service.quota_summary()['hourly']['limit']==30


async def test_retry_expiry_rechecked_inside_admission_before_any_start(env,monkeypatch):
    first=view(env,fetched=source(('one',)))
    await finish(env)
    service=env['app'].state.service
    before=service.quota_summary()['hourly']['used']
    original=service._admit
    def expired_admission(conn,*args,**kwargs):
        conn.execute('UPDATE feed_views SET expires_at=0 WHERE id=?',(first['view']['id'],))
        return original(conn,*args,**kwargs)
    monkeypatch.setattr(service,'_admit',expired_admission)
    response=env['client'].post('/api/runs/'+first['runs'][0]['id']+'/retry?view_id='+first['view']['id'],json={'idempotency_key':'cross-boundary-retry'},headers=env['headers'])
    assert response.status_code==404 and response.json()['error']['code']=='VIEW_NOT_FOUND'
    assert service.quota_summary()['hourly']['used']==before
    assert len(env['db'].all('SELECT * FROM runs'))==1
