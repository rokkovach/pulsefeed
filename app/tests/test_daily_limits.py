from concurrent.futures import ThreadPoolExecutor
import json
import time
import pytest
from pulsefeed.auth import issue_link,seed_account
from pulsefeed.db import Database,DEFAULT_SETTINGS,dump,new_id
from pulsefeed.errors import AppError
from pulsefeed.rss import canonical_feed_id
from pulsefeed.service import Service
from test_cli import command
from test_jobs import finish
from test_view_feed import source


def ledger(env,owner,url,amount=2,at=None):
    service=env['app'].state.service
    item=service.save_feed(owner,url,source(('ledger',)))['items'][0]
    now=time.time() if at is None else at
    batch_id,run_id=new_id(),new_id()
    epoch=env['db'].one('SELECT reset_epoch FROM quota_policy')['reset_epoch']
    env['db'].execute('INSERT INTO batches VALUES(?,?,?,?,?)',(batch_id,owner,new_id(),'finished',now))
    original=dict(item,article={'title':item['title'],'excerpt':item['excerpt']})
    env['db'].execute("INSERT INTO runs(id,batch_id,owner,item_id,ordinal,state,settings,original,seed,created,reserved,reserved_at,quota_finalized_at,quota_epoch,admission_epoch) VALUES(?,?,?,?,0,'completed',?,?,1,?,?,?,?,?,?)",(run_id,batch_id,owner,item['id'],dump(DEFAULT_SETTINGS),dump(original),now,amount,now,now,epoch,epoch))
    output={'model':{'id':'black-forest-labs/FLUX.2-klein-4B','revision':'synthetic-fixture','precision':'synthetic','runtime':'synthetic-fixture'},'usage':{'image_count':1},'counters':{'inference_attempts':amount,'inference_retries':max(0,amount-1),'dependency_retries':0}}
    env['db'].execute("INSERT INTO invocations(id,run_id,stage,status,input,output,created,execution_mode) VALUES(?,?,'flux','succeeded','{}',?,?,'synthetic')",(new_id(),run_id,dump(output),now))
    return run_id,item['id']


def settle(env,run_id,amount=2):
    row=env['db'].one('SELECT * FROM runs WHERE id=?',(run_id,))
    output={'counters':{'inference_attempts':amount}}
    env['db'].execute("INSERT INTO invocations(id,run_id,stage,status,input,output,created,execution_mode) VALUES(?,?,'flux','succeeded','{}',?,?,'synthetic')",(new_id(),run_id,dump(output),time.time()))
    env['app'].state.coordinator.finish(row,reserved=amount)


def admin(env):
    env['client'].get(issue_link(env['db'],env['config'],env['admin']).replace(env['config'].base_url,''))
    return {'X-CSRF-Token':env['client'].get('/api/session').json()['csrf']}


def test_default_policy_persistence_and_seed_only(tmp_path,env):
    summary=env['app'].state.service.quota_summary()
    assert summary['global']['limit']==100 and summary['page']['article_limit']==10
    path=str(tmp_path/'policy.db')
    db=Database(path,12,4)
    assert db.one('SELECT global_daily_limit,feed_article_limit FROM quota_policy')=={'global_daily_limit':12,'feed_article_limit':4}
    db=Database(path,999,99)
    assert db.one('SELECT global_daily_limit,feed_article_limit FROM quota_policy')=={'global_daily_limit':12,'feed_article_limit':4}
    env['app'].state.service.set_limits(env['admin'],{'global_daily_limit':8,'feed_article_limit':6})
    Database(env['config'].database_path,100,10)
    assert env['app'].state.service.quota_summary()['global']['limit']==8


def test_canonical_identity_preserves_path_and_query():
    first=canonical_feed_id('HTTPS://Example.COM:443/feed?a=1&b=2#section')
    assert first==canonical_feed_id('https://example.com/feed?a=1&b=2')
    assert canonical_feed_id('http://EXAMPLE.com:80')==canonical_feed_id('http://example.com/#fragment')
    for other in ('http://example.com/feed?a=1&b=2','https://example.com/Feed?a=1&b=2','https://example.com/feed?b=2&a=1','https://example.com/feed?a=2&b=2'):
        assert first!=canonical_feed_id(other)


def test_same_canonical_feed_has_no_daily_gate_for_new_views_or_retries(env):
    service=env['app'].state.service
    previous=None
    for _ in range(6):previous,_=ledger(env,env['owner'],'HTTPS://Example.COM:443/feed#first')
    data=service.view_feed(env['other'],'https://example.com/feed','fresh-same-feed',source())
    assert len(data['runs'])==5 and data['generation']['status']=='started'
    assert service.quota_summary()['global']['charged']==22
    assert service.quota_summary()['page']=={'article_limit':10,'max_articles':10}
    assert 'per_feed' not in service.quota_summary()
    for run in data['runs']:settle(env,run['id'])
    item_id=env['db'].one('SELECT item_id FROM runs WHERE id=?',(previous,))['item_id']
    retry=service.admit(env['owner'],[item_id],'retry-same-canonical',previous)
    assert len(retry['runs'])==1
    assert env['provider'].submissions==[]


def test_global_hundred_boundary_across_accounts(env):
    service=env['app'].state.service
    env['db'].execute('UPDATE quota_policy SET global_hourly_generation_limit=100')
    for index in range(49):
        owner=env['owner'] if index//5<5 else env['other']
        ledger(env,owner,'https://example.com/feed-'+str(index//5))
    assert service.quota_summary()['global']['charged']==98
    item=service.save_feed(env['admin'],'https://example.com/new',source(('new',)))['items'][0]
    batch=service.admit(env['admin'],[item['id']],'global-last-two')
    assert service.quota_summary()['global']['charged']==100
    settle(env,batch['runs'][0]['id'])
    late=seed_account(env['db'],'late','evaluator')
    item=service.save_feed(late,'https://example.com/late',source(('late',)))['items'][0]
    with pytest.raises(AppError) as failure:service.admit(late,[item['id']],'global-overflow')
    assert failure.value.code=='GLOBAL_DAILY_LIMIT' and failure.value.retry_after>0
    assert service.quota_summary()['global']['remaining']==0


def test_concurrent_global_reservations_cannot_exceed_hundred(env):
    service=env['app'].state.service
    env['db'].execute('UPDATE quota_policy SET global_hourly_generation_limit=100')
    for index in range(49):
        ledger(env,env['owner'] if index//5<5 else env['other'],'https://example.com/seed-'+str(index//5))
    owners=[env['admin'],seed_account(env['db'],'concurrent','evaluator')]
    items=[service.save_feed(owner,'https://example.com/fresh-'+str(index),source(('fresh',)))['items'][0]['id'] for index,owner in enumerate(owners)]
    def reserve(index):
        try:return service.admit(owners[index],[items[index]],'concurrent-global-'+str(index))['id']
        except AppError as exc:return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(reserve,[0,1]))
    assert results.count('BUSY')==1
    assert service.quota_summary()['global']['charged']==100
    assert service.quota_summary()['global']['reserved']==2


async def test_actual_worker_counts_refund_reserved_and_replay_is_free(env):
    service=env['app'].state.service
    data=service.view_feed(env['owner'],'demo','accounting-view',source(('one',)))
    assert service.quota_summary()['global']['reserved']==2
    await finish(env)
    before=service.quota_summary()['global']
    assert before['charged']==before['used']==1 and before['reserved']==0
    service.cached_view(env['owner'],'accounting-view','demo')
    service.feed(env['owner'],data['feed']['id'])
    service.run(env['owner'],data['runs'][0]['id'])
    after=service.quota_summary()['global']
    assert {k:after[k] for k in ('charged','used','reserved')}=={k:before[k] for k in ('charged','used','reserved')}
    assert len(env['provider'].submissions)==2


async def test_unknown_counters_are_conservative_not_known_usage(env):
    original=env['provider'].status
    async def status(stage,provider_id):
        response=await original(stage,provider_id)
        if stage=='flux' and response.get('output'):
            response['output']['result']['counters'].update(inference_attempts=None,inference_retries=None)
        return response
    env['provider'].status=status
    service=env['app'].state.service
    service.view_feed(env['owner'],'demo','unknown-counter',source(('one',)))
    await finish(env)
    summary=service.quota_summary()['global']
    assert summary['charged']==2 and summary['used']==0 and summary['uncertain_charged']==2 and summary['reserved']==0


async def test_pending_midnight_reservation_and_late_completion_stay_charged(env,monkeypatch):
    service=env['app'].state.service
    start=20000*86400+86390
    monkeypatch.setattr(time,'time',lambda:start)
    service.view_feed(env['owner'],'demo','midnight-view',source(('one',)))
    next_day=start+20
    monkeypatch.setattr(time,'time',lambda:next_day)
    assert service.quota_summary()['global']['reserved']==2
    await finish(env)
    summary=service.quota_summary()['global']
    assert summary['charged']==summary['used']==1 and summary['reserved']==0
    assert env['db'].one('SELECT quota_finalized_at FROM runs')['quota_finalized_at']==next_day
    assert service.quota_summary(now=next_day+86400)['global']['charged']==0


async def test_reset_same_timestamp_keeps_active_then_late_settlement_and_history(env,monkeypatch):
    service=env['app'].state.service
    now=20000*86400+1000
    monkeypatch.setattr(time,'time',lambda:now)
    ledger(env,env['owner'],'https://example.com/old',1)
    data=service.view_feed(env['owner'],'demo','reset-active-1',source(('one',)))
    counts=env['db'].one('SELECT COUNT(*) AS count FROM runs')['count'],env['db'].one('SELECT COUNT(*) AS count FROM invocations')['count']
    assert service.quota_summary()['global']['charged']==3
    summary=service.reset_limits(env['admin'],{'confirm':True})['global']
    assert summary['charged']==summary['reserved']==2 and summary['used']==0
    assert summary['window_start']==now and summary['reset_at']==20001*86400
    assert (env['db'].one('SELECT COUNT(*) AS count FROM runs')['count'],env['db'].one('SELECT COUNT(*) AS count FROM invocations')['count'])==counts
    await finish(env)
    summary=service.quota_summary()['global']
    assert summary['charged']==summary['used']==1 and summary['reserved']==0
    assert env['db'].one('SELECT quota_epoch FROM runs WHERE id=?',(data['runs'][0]['id'],))['quota_epoch']==env['db'].one('SELECT reset_epoch FROM quota_policy')['reset_epoch']
    assert env['db'].one('SELECT COUNT(*) AS count FROM quota_audit')['count']==1


async def test_unknown_reset_then_cli_resolution_keeps_charge_today(env):
    service=env['app'].state.service
    env['provider'].behavior['qwen']='unknown'
    data=service.view_feed(env['owner'],'demo','unknown-reset',source(('one',)))
    await env['app'].state.coordinator.tick()
    service.reset_limits(env['admin'],{'confirm':True})
    assert service.quota_summary()['global']['reserved']==2
    command(env,'resolve','--run',data['runs'][0]['id'],'--confirm-stopped')
    summary=service.quota_summary()['global']
    assert summary['charged']==2 and summary['uncertain_charged']==2 and summary['reserved']==0


def test_admin_access_csrf_limits_reset_audit_and_rate(env):
    client=env['client']
    login = client.get('/admin')
    assert login.status_code==200 and login.template.name=='admin_login.html'
    assert 'settings' not in login.context and 'usage' not in login.context
    assert client.get('/api/admin/usage').status_code==403
    assert client.post('/api/admin/limits',json={'global_daily_limit':8,'feed_article_limit':4},headers=env['headers']).status_code==403
    assert client.post('/api/admin/limits/reset',json={'confirm':True},headers=env['headers']).status_code==403
    headers=admin(env)
    assert client.get('/admin').status_code==200
    assert client.get('/api/admin/usage?window=bad').status_code==400
    assert client.post('/api/admin/limits/reset',json={'confirm':True}).status_code==403
    response=client.post('/api/admin/limits',json={'global_daily_limit':8,'feed_article_limit':4},headers=headers)
    assert response.status_code==200 and response.json()['quotas']['global']['limit']==8
    assert client.post('/api/admin/limits/reset',json={'confirm':False},headers=headers).status_code==400
    assert client.post('/api/admin/limits/reset',json={'confirm':True},headers=headers).status_code==200
    assert [row['event'] for row in env['db'].all('SELECT event FROM quota_audit')]==['limits_updated','allowance_reset']
    env['config'].admin_write_limit=1
    response=client.post('/api/admin/limits/reset',json={'confirm':True},headers=headers)
    assert response.status_code==429 and response.headers['retry-after']


@pytest.mark.parametrize('body',[{'global_daily_limit':0,'feed_article_limit':1},{'global_daily_limit':10001,'feed_article_limit':1},{'global_daily_limit':100,'feed_article_limit':11},{'global_daily_limit':2,'feed_article_limit':0},{'global_daily_limit':True,'feed_article_limit':1},{'global_daily_limit':100,'feed_article_limit':1,'extra':True}])
def test_admin_limit_input_bounds(env,body):
    response=env['client'].post('/api/admin/limits',json=body,headers=admin(env))
    assert response.status_code==400
    assert env['db'].all('SELECT * FROM quota_audit')==[]


def test_daily_denial_direct_429_but_view_still_readable(env):
    service=env['app'].state.service
    service.set_limits(env['admin'],{'global_daily_limit':1,'feed_article_limit':1})
    viewed=env['client'].post('/api/view-feed',json={'url':'demo','idempotency_key':'limited-view'},headers=env['headers'])
    assert viewed.status_code==200 and len(viewed.json()['items'])==3
    assert viewed.json()['generation']['error']['code']=='GLOBAL_DAILY_LIMIT'
    item=viewed.json()['items'][0]['id']
    direct=env['client'].post('/api/batches',json={'item_ids':[item],'idempotency_key':'limited-direct'},headers=env['headers'])
    assert direct.status_code==404  # Manual reader admission has been retired.
    with pytest.raises(AppError) as denial:
        service.admit(env['owner'],[item],'limited-internal')
    assert denial.value.status==429 and denial.value.retry_after
    assert env['provider'].submissions==[]


def test_ten_item_cap_injected_current_cached_and_legacy_paths(env):
    service=env['app'].state.service
    fetched=source(tuple('item-'+str(i) for i in range(13)))
    data=service.view_feed(env['owner'],'demo','ten-item-feed',fetched)
    assert len(data['items'])==10 and len(env['db'].all('SELECT * FROM items'))==10
    assert len(env['db'].all('SELECT * FROM feed_current_items'))==10
    assert len(json.loads(env['db'].one('SELECT items_snapshot FROM feed_views')['items_snapshot']))==10
    # Historical records/snapshots created before the cap are bounded on reads, retained in storage.
    feed_id=data['feed']['id'];extra=[]
    for index in range(10,13):
        item=fetched['items'][index];item_id=new_id()
        env['db'].execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?)',(item_id,feed_id,item['guid'],item['title'],item['excerpt'],item['link'],item['published'],dump(item['article'])))
        extra.append(dict(item,id=item_id))
    snapshot=data['items']+extra
    env['db'].execute('UPDATE feed_views SET items_snapshot=?',(dump(snapshot),))
    assert len(service.cached_view(env['owner'],'ten-item-feed','demo')['items'])==10
    env['db'].execute('DELETE FROM feed_current_items');env['db'].execute('DELETE FROM feed_current_state')
    assert len(service.feed(env['owner'],feed_id)['items'])==10
    assert len(env['db'].all('SELECT * FROM items'))==13


async def test_new_invocations_record_mode_and_admin_aggregate_is_private(env):
    service=env['app'].state.service
    sentinel='PRIVATE_VISITOR_SENTINEL'
    private=source((sentinel,))
    private['items'][0]['excerpt']=sentinel+' excerpt'
    data=service.view_feed(env['owner'],'https://example.com/'+sentinel,'private-view-key',private)
    await finish(env)
    env['db'].execute('UPDATE runs SET qwen_prompt=?,final_prompt=?',(sentinel+' prompt',sentinel+' final'))
    assert {row['execution_mode'] for row in env['db'].all('SELECT execution_mode FROM invocations')}=={'synthetic'}
    headers=admin(env)
    for path in ('/api/admin/usage','/api/admin/usage?window=all'):
        response=env['client'].get(path)
        assert response.status_code==200
        text=response.text
        assert sentinel not in text and data['feed']['id'] not in text and data['runs'][0]['id'] not in text
        assert all(account not in text for account in (env['owner'],env['other'],env['admin']))
        assert 'quotas' in response.json() and response.json()['models']
    html=env['client'].get('/admin').text
    assert sentinel not in html
    assert len(env['provider'].submissions)==2


def test_schema_two_upgrade_preserves_history_and_unknown_provenance(tmp_path):
    import sqlite3
    from pulsefeed.db import SCHEMA
    path=str(tmp_path/'legacy.db')
    old=SCHEMA.replace(',canonical_id TEXT','').replace(',quota_finalized_at REAL,quota_epoch INTEGER NOT NULL DEFAULT 0','').replace(',execution_mode TEXT','')
    old='\n'.join(line for line in old.splitlines() if not line.startswith('CREATE TABLE IF NOT EXISTS quota_'))
    conn=sqlite3.connect(path)
    conn.executescript(old)
    owner,feed,item,batch,run,invocation=[new_id() for _ in range(6)]
    conn.execute('INSERT INTO accounts VALUES(?,?,?,0)',(owner,'legacy','evaluator'))
    conn.execute('INSERT INTO feeds VALUES(?,?,?,?,0)',(feed,owner,'HTTPS://Example.com:443/rss#old','Legacy'))
    conn.execute("INSERT INTO items VALUES(?,?,?,'Legacy title','','https://example.com/story','','{}')",(item,feed,'guid'))
    conn.execute("INSERT INTO batches VALUES(?,?,?,'finished',0)",(batch,owner,'legacy-key'))
    conn.execute("INSERT INTO runs(id,batch_id,owner,item_id,ordinal,state,settings,original,seed,created,reserved,reserved_at) VALUES(?,?,?,?,0,'failed',?,?,1,0,2,0)",(run,batch,owner,item,dump(DEFAULT_SETTINGS),'{}'))
    conn.execute("INSERT INTO invocations(id,run_id,stage,status,input,created) VALUES(?,?,'flux','failed','{}',0)",(invocation,run))
    conn.commit();conn.close()
    db=Database(path,20,4)
    assert db.one('SELECT canonical_id FROM feeds')['canonical_id']==canonical_feed_id('https://example.com/rss')
    assert db.one('SELECT quota_finalized_at,quota_epoch FROM runs')=={'quota_finalized_at':0.0,'quota_epoch':0}
    assert db.one('SELECT execution_mode FROM invocations')['execution_mode'] is None
    assert db.one('PRAGMA user_version')['user_version']==7
    assert db.one('SELECT global_daily_limit,feed_article_limit FROM quota_policy')=={'global_daily_limit':20,'feed_article_limit':4}
    Database(path,99,9)
    assert db.one('SELECT global_daily_limit FROM quota_policy')['global_daily_limit']==20


def test_admin_endpoints_require_authentication(env):
    env['db'].execute('UPDATE sessions SET revoked=1')
    login = env['client'].get('/admin')
    assert login.status_code==200 and login.template.name=='admin_login.html'
    assert 'settings' not in login.context and 'usage' not in login.context
    assert env['client'].get('/api/admin/usage').status_code==401
    for path,body in (('/api/admin/limits',{'global_daily_limit':100,'feed_article_limit':10}),('/api/admin/limits/reset',{'confirm':True})):
        assert env['client'].post(path,json=body,headers=env['headers']).status_code==401
