from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import pytest
from pulsefeed.auth import issue_link
from pulsefeed.db import Database,dump,new_id
from pulsefeed.errors import AppError
from pulsefeed.main import create_app
from pulsefeed.provider import FakeRunpodAdapter
from pulsefeed.rss import parse_feed
from pulsefeed.service import Service
from test_jobs import finish


def source(labels=('one','two','three','four','five')):
    return {'title':'Current source','items':[{'guid':hashlib.sha256(label.encode()).hexdigest(),'title':label,'excerpt':'Original '+label,'link':'https://example.com/'+label,'published':'','article':{'title':label,'excerpt':'Original '+label}} for label in labels]}


def post(env,key='view-key-1',url='demo'):
    return env['client'].post('/api/view-feed',json={'url':url,'idempotency_key':key},headers=env['headers'])


def test_view_api_shape_read_restore_and_validation(env):
    response=post(env)
    assert response.status_code==200,response.text
    data=response.json()
    assert set(data)=={'feed','items','runs','active_batch_id','generation','view','server_now'}
    assert set(data['generation'])=={'status','batch','error','retry_after'}
    assert data['generation']['status']=='started' and len(data['runs'])==3
    assert [item['title'] for item in data['items']]==['A new community garden opens','Scientists explore ocean habitats','A library welcomes its readers']
    assert env['provider'].submissions==[]  # Admission is prompt; coordinator runs later.
    for _ in range(2):
        restored=env['client'].get(data['view']['url']).context['initial_feed']
        assert restored['items']==data['items'] and len(restored['runs'])==3
        env['client'].get('/')
    assert len(env['db'].all('SELECT * FROM runs'))==3 and env['provider'].submissions==[]
    assert post(env,url='https://example.com/changed').status_code==409
    assert post(env,key='short').status_code==400
    assert post(env,key='valid-key-9',url='file:///etc/passwd').status_code==400
    assert env['client'].post('/api/view-feed',json={'url':'demo','idempotency_key':'valid-key-9','extra':True},headers=env['headers']).status_code==400
    assert env['client'].post('/api/view-feed',json={'url':'demo','idempotency_key':'valid-key-9'}).status_code==403


async def test_affordable_prefix_and_repeat_keys_never_drain_later_items(env):
    service=env['app'].state.service
    fetched=source(('one','two','three','four','five','six'))
    data=service.view_feed(env['owner'],'demo','source-key-1',fetched)
    assert [item['title'] for item in data['items']]==['one','two','three','four','five','six']
    assert [run['original']['title'] for run in data['runs']]==['one','two','three','four','five','six']
    await finish(env)
    replay=service.view_feed(env['owner'],'demo','source-key-1',source())
    assert replay['generation']['status']=='reused'
    data=service.view_feed(env['owner'],'demo','source-key-2',fetched)
    assert data['generation']['status']=='started' and len(data['runs'])==6
    assert [run['original']['title'] for run in data['runs']]==['one','two','three','four','five','six']
    await finish(env)
    assert len(service.cached_view(env['owner'],'source-key-1','demo')['runs'])==6
    assert len(env['db'].all('SELECT * FROM runs'))==12
    assert len(env['provider'].submissions)==24
    assert env['db'].all("SELECT r.id FROM runs r JOIN items i ON i.id=r.item_id WHERE i.title='six'")


def test_concurrent_distinct_and_duplicate_keys_admit_once(env):
    service=env['app'].state.service
    def view(key):return service.view_feed(env['owner'],'demo',key,source())
    with ThreadPoolExecutor(max_workers=3) as pool:
        results=list(pool.map(view,['parallel-key-1','parallel-key-2','parallel-key-1']))
    assert [r['generation']['status'] for r in results].count('started')==1
    assert len(env['db'].all('SELECT * FROM batches'))==1
    assert len(env['db'].all('SELECT * FROM runs'))==5
    assert len(env['db'].all('SELECT * FROM feed_views'))==2
    assert all(r['active_batch_id'] is None for r in results if r['generation']['status']=='blocked')


async def test_existing_charge_does_not_reduce_page_article_limit(env):
    service=env['app'].state.service
    saved=service.save_feed(env['owner'],'demo',source())
    old=service.admit(env['owner'],[saved['items'][0]['id']],'explicit-first-1')
    await finish(env)
    data=service.view_feed(env['owner'],'demo','partial-view-1',source())
    assert data['generation']['status']=='started'
    assert [run['original']['title'] for run in data['generation']['batch']['runs']]==['one','two','three','four','five']
    assert len(data['runs'])==5
    assert len(env['db'].all('SELECT * FROM runs'))==6


async def test_membership_order_changes_preserve_old_run_rows(env):
    service=env['app'].state.service
    first=service.view_feed(env['owner'],'demo','membership-key-1',source())
    await finish(env)
    second=service.view_feed(env['owner'],'demo','membership-key-2',source(('five','three','six','one')))
    assert [item['title'] for item in second['items']]==['five','three','six','one']
    assert [run['original']['title'] for run in second['generation']['batch']['runs']]==['five','three','six','one']
    restored=service.feed(env['owner'],first['feed']['id'])
    assert [item['title'] for item in restored['items']]==['five','three','six','one']
    assert env['db'].one("SELECT id FROM items WHERE title='two'")
    assert env['db'].one("SELECT r.id FROM runs r JOIN items i ON i.id=r.item_id WHERE i.title='two'")
    # The first request's durable response keeps its own original source membership/order.
    replay=service.cached_view(env['owner'],'membership-key-1','demo')
    assert [item['title'] for item in replay['items']]==['one','two','three','four','five']
    assert replay['generation']['batch']['id']==first['generation']['batch']['id']


async def test_cached_request_survives_restart_and_no_refetch(env):
    response=post(env);data=response.json()
    await finish(env)
    restarted=Service(Database(env['config'].database_path),env['config'])
    replay=restarted.cached_view(env['owner'],'view-key-1','demo')
    assert replay['generation']['status']=='reused'
    assert replay['generation']['batch']['id']==data['generation']['batch']['id']
    assert all(run['state']=='completed' for run in replay['runs'])
    env['config'].rss_limit=1  # The first view has already consumed the one permitted fetch.
    assert post(env).status_code==200
    assert len(env['db'].all('SELECT * FROM runs'))==3
    assert len(env['provider'].submissions)==6


def test_busy_and_quota_keep_readable_feed_and_cache_blocked_outcome(env):
    service=env['app'].state.service
    service.view_feed(env['owner'],'other-feed','other-view-1',source(('active',)))
    data=service.view_feed(env['owner'],'demo','blocked-view-1',source())
    assert len(data['items'])==5 and data['generation']['status']=='blocked'
    assert data['generation']['error']['code']=='BUSY' and data['generation']['retry_after']==5
    env['db'].execute("UPDATE runs SET state='failed'")
    env['db'].execute("UPDATE batches SET state='finished'")
    env['config'].image_quota=2
    data=service.view_feed(env['owner'],'demo','quota-view-1',source())
    assert len(data['items'])==5 and data['generation']['status']=='blocked'
    assert data['generation']['error']['code']=='QUOTA_EXCEEDED'
    env['config'].image_quota=30
    assert service.cached_view(env['owner'],'quota-view-1','demo')['generation']['status']=='blocked'
    assert len(env['db'].all('SELECT * FROM runs'))==1


def test_api_blocked_still_returns_200_readable_feed(env):
    env['config'].image_quota=1
    response=post(env)
    assert response.status_code==200
    assert len(response.json()['items'])==3
    assert response.json()['generation']['status']=='blocked'
    assert response.json()['generation']['error']['code']=='QUOTA_EXCEEDED'
    assert env['provider'].submissions==[]


async def test_latest_runs_older_than_history_window_and_failed_unknown_expired_reused(env):
    service=env['app'].state.service
    data=service.view_feed(env['owner'],'demo','old-runs-key-1',source(('one','two','three')))
    await finish(env)
    ids=[run['id'] for run in data['runs']]
    env['db'].execute("UPDATE runs SET state='failed',created=1 WHERE id=?",(ids[0],))
    env['db'].execute("UPDATE runs SET state='submission_unknown',created=1 WHERE id=?",(ids[1],))
    env['db'].execute("UPDATE runs SET result_expires_at=0,created=1 WHERE id=?",(ids[2],))
    # Fill the general history's 30-row window with later attempts on another feed item.
    extra=service.save_feed(env['owner'],'extra',source(('extra',)))['items'][0]['id']
    original=env['db'].one('SELECT * FROM runs WHERE id=?',(ids[0],))
    for index in range(31):
        values=dict(original,id=new_id(),item_id=extra,created=100+index)
        columns=list(values)
        env['db'].execute('INSERT INTO runs ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',tuple(values.values()))
    assert not set(ids)&{run['id'] for run in service.history(env['owner'])['runs']}
    reused=service.cached_view(env['owner'],'old-runs-key-1','demo')
    assert {run['id'] for run in reused['runs']}==set(ids)
    assert reused['generation']['status']=='reused'
    assert len(env['db'].all('SELECT * FROM runs'))==34
    assert len(env['provider'].submissions)==6


def test_empty_membership_and_deterministic_legacy_fallback(env):
    service=env['app'].state.service
    saved=service.save_feed(env['owner'],'demo',source())
    feed_id=saved['feed']['id']
    env['db'].execute('DELETE FROM feed_current_items')
    env['db'].execute('DELETE FROM feed_current_state')
    assert [item['title'] for item in service.feed(env['owner'],feed_id)['items']]==['one','two','three','four','five']
    empty=service.view_feed(env['owner'],'demo','empty-feed-key',source(()))
    assert empty['items']==[] and empty['runs']==[] and empty['generation']['status']=='idle'
    assert service.feed(env['owner'],feed_id)['items']==[]
    assert len(env['db'].all('SELECT * FROM items'))==5


def test_source_items_are_capped_at_ten_before_persistence():
    xml='<rss><channel><title>All</title>'+''.join(f'<item><guid>{i}</guid><title>Story {i}</title></item>' for i in range(125))+'</channel></rss>'
    parsed=parse_feed(xml.encode(),'https://example.com/rss')
    assert len(parsed['items'])==10 and parsed['items'][-1]['title']=='Story 9'


def test_admin_query_role_matrix(env):
    client=env['client']
    viewer=client.get('/')
    assert viewer.status_code==200 and viewer.context['admin_mode'] is False and viewer.context['settings'] is None
    denied=client.get('/?admin')
    assert denied.status_code==403 and denied.template.name=='access.html'
    client.get(issue_link(env['db'],env['config'],env['admin']).replace(env['config'].base_url,''))
    viewer=client.get('/')
    assert viewer.status_code==200 and not viewer.context['admin_mode'] and viewer.context['settings'] is None
    for query in ('?admin','?admin=true','?admin=false'):
        admin=client.get('/'+query)
        assert admin.status_code==200 and admin.context['admin_mode'] and admin.context['settings']
    assert client.get('/?mode=admin').context['admin_mode'] is False


async def test_raw_first_ten_cap_precedes_duplicate_guid_collapse(env):
    service=env['app'].state.service
    fetched=source(('one',)*9+('two','three'))
    data=service.view_feed(env['owner'],'demo','duplicate-source-1',fetched)
    assert [run['original']['title'] for run in data['generation']['batch']['runs']]==['one','two']
    await finish(env)
    repeated=service.view_feed(env['owner'],'demo','duplicate-source-1',fetched)
    assert repeated['generation']['status']=='reused'
    assert len(env['db'].all('SELECT * FROM runs'))==2
    assert not env['db'].all("SELECT r.id FROM runs r JOIN items i ON i.id=r.item_id WHERE i.title='three'")


def test_malformed_feed_url_returns_safe_400(env):
    response=post(env,key='bad-url-key',url='http://[')
    assert response.status_code==400
    assert response.json()['error']['code']=='INVALID_FEED'
    from fastapi.testclient import TestClient
    app=create_app(env['config'],FakeRunpodAdapter())
    with TestClient(app) as client:
        client.cookies.update(env['client'].cookies)
        response=client.post('/api/feeds',json={'url':'http://['},headers=env['headers'])
        assert response.status_code==404 and response.json()['error']['code']=='VIEW_NOT_FOUND'
    from pulsefeed.rss import public_url,safe_link
    with pytest.raises(AppError):public_url('http://[')
    assert safe_link('http://[','https://example.com/feed')==''
    assert env['provider'].submissions==[]
