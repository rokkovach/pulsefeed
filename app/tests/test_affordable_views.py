"""New reader pages admit one affordable source prefix; older pages never drain."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from pulsefeed.errors import AppError
from test_daily_limits import ledger
from test_jobs import finish
from test_view_feed import source

URL = 'https://example.com/affordable-feed'


def stories(count=12):
    return source(tuple('story-'+str(index) for index in range(count)))


def policy(env, global_limit=100, page_limit=10, hourly_limit=30):
    env['db'].execute('UPDATE quota_policy SET global_daily_limit=?,feed_article_limit=?,global_hourly_generation_limit=?',
                      (global_limit,page_limit,hourly_limit))


def view(env,key='affordable-view',owner=None,url=URL):
    return env['app'].state.service.view_feed(owner or env['owner'],url,key,stories())


def assert_prefix(data,count):
    assert data['generation']['status']=='started'
    assert len(data['items'])==10
    assert [run['original']['title'] for run in data['runs']]==['story-'+str(index) for index in range(count)]
    assert [run['id'] for run in data['generation']['batch']['runs']]==[run['id'] for run in data['runs']]


def test_sufficient_allowances_admit_ten_source_slots_and_never_position_eleven(env):
    policy(env)
    data=view(env)
    assert_prefix(data,10)
    assert env['app'].state.service.quota_summary()['global']['reserved']==20
    assert len(env['db'].all('SELECT * FROM runs'))==10
    assert [row['ordinal'] for row in env['db'].all('SELECT ordinal FROM runs ORDER BY ordinal')]==list(range(10))
    assert not env['db'].one("SELECT id FROM items WHERE title='story-10'")
    assert env['provider'].submissions==[]


def test_default_page_limit_admits_ten_runs_with_two_reserved_each(env):
    data=view(env)
    assert_prefix(data,10)
    summary=env['app'].state.service.quota_summary()
    assert summary['global']['reserved']==20 and summary['hourly']['used']==10
    assert summary['page']=={'article_limit':10,'max_articles':10}


@pytest.mark.parametrize('limiter,expected', [('owner',2),('hourly',2),('global',2),('page',2)])
def test_partial_allowance_selects_the_maximum_fitting_prefix_across_every_budget(env,limiter,expected):
    policy(env)
    if limiter=='owner':
        env['config'].image_quota=9
        ledger(env,env['owner'],'https://other.example/previous',2)
        ledger(env,env['owner'],'https://other.example/previous',2)
    elif limiter=='hourly':
        policy(env,hourly_limit=4)
        ledger(env,env['other'],'https://other.example/previous',1)
        ledger(env,env['other'],'https://other.example/previous',1)
    elif limiter=='global':
        policy(env,global_limit=10,page_limit=10)
        for _ in range(3):ledger(env,env['other'],'https://other.example/previous',2)
    else:
        policy(env,page_limit=2)
        for _ in range(3):ledger(env,env['other'],'https://EXAMPLE.com:443/affordable-feed#earlier-visitor',2)
    data=view(env)
    assert_prefix(data,expected)
    assert len(data['generation']['batch']['runs'])==expected
    assert env['provider'].submissions==[]


@pytest.mark.parametrize('limiter,code', [('owner','QUOTA_EXCEEDED'),('hourly','GLOBAL_HOURLY_LIMIT'),
                                         ('global','GLOBAL_DAILY_LIMIT'),('busy','BUSY')])
def test_zero_capacity_stays_blocked_with_authoritative_reason_and_replay_does_not_retry(env,limiter,code):
    policy(env)
    if limiter=='owner':env['config'].image_quota=1
    elif limiter=='hourly':
        policy(env,hourly_limit=1)
        ledger(env,env['other'],'https://other.example/previous',1)
    elif limiter=='global':policy(env,global_limit=1,page_limit=1)
    else:
        env['app'].state.service.view_feed(env['other'],'https://other.example/active','existing-active',source(('active',)))
    before=len(env['db'].all('SELECT * FROM runs'))
    data=view(env)
    assert data['generation']['status']=='blocked' and data['generation']['error']['code']==code
    assert data['generation']['batch'] is None and data['runs']==[] and data['active_batch_id'] is None
    assert data['generation']['retry_after']>0
    replay=env['app'].state.service.cached_view(env['owner'],'affordable-view',URL)
    assert replay['generation']['error']==data['generation']['error'] and replay['view']==data['view']
    assert len(env['db'].all('SELECT * FROM runs'))==before
    assert env['provider'].submissions==[]


def test_concurrent_keys_and_replay_cannot_oversubscribe_last_hourly_slots(env):
    policy(env,hourly_limit=2)
    def admit(pair):return view(env,key=pair[0],owner=pair[1])
    with ThreadPoolExecutor(max_workers=3) as pool:
        results=list(pool.map(admit,[('concurrent-budget-a',env['owner']),('concurrent-budget-b',env['other']),('concurrent-budget-a',env['owner'])]))
    assert sum(result['generation']['status']=='started' for result in results)==1
    assert len(env['db'].all('SELECT * FROM runs'))==2
    assert env['app'].state.service.quota_summary()['global']['reserved']==4
    assert env['app'].state.service.quota_summary()['hourly']['used']==2
    assert len(env['db'].all("SELECT * FROM batches WHERE state='active'"))==1
    assert env['provider'].submissions==[]


async def test_historical_three_run_page_and_exact_key_remain_frozen_when_new_views_can_admit_ten(env):
    policy(env)
    env['config'].image_quota=6
    old=view(env)
    assert_prefix(old,3)
    await finish(env)
    submitted=len(env['provider'].submissions)
    env['config'].image_quota=30
    service=env['app'].state.service
    for _ in range(2):
        restored=service.reader_view(env['owner'],old['view']['id'])
        replay=service.cached_view(env['owner'],'affordable-view',URL)
        assert restored['view']==replay['view']==old['view']
        assert [run['id'] for run in restored['runs']]==[run['id'] for run in old['runs']]
        assert len(replay['runs'])==3 and restored['active_batch_id'] is None
    assert len(env['provider'].submissions)==submitted
    new=view(env,key='larger-new-page')
    assert_prefix(new,10)
    assert set(run['id'] for run in new['runs']).isdisjoint(run['id'] for run in old['runs'])
    assert len(service.reader_view(env['owner'],old['view']['id'])['runs'])==3
    assert len(env['provider'].submissions)==submitted


async def test_released_attempts_do_not_automatically_drain_more_stories_or_bypass_retry_limits(env):
    env['provider'].behavior['qwen']='failure'
    data=view(env)
    assert_prefix(data,10)
    await finish(env)
    service=env['app'].state.service
    assert service.quota_summary()['global']['charged']==0
    assert len(service.cached_view(env['owner'],'affordable-view',URL)['runs'])==10
    assert len(service.reader_view(env['owner'],data['view']['id'])['runs'])==10
    assert len(env['db'].all('SELECT * FROM runs'))==10
    assert len(env['provider'].submissions)==10
    env['config'].image_quota=1
    with pytest.raises(AppError) as error:
        service.retry_view(env['owner'],data['view']['id'],data['runs'][0]['id'],'retry-still-bounded')
    assert error.value.code=='QUOTA_EXCEEDED'
    assert len(env['db'].all('SELECT * FROM runs'))==10


def test_legacy_manual_selection_limit_remains_one_to_three(env):
    policy(env)
    service=env['app'].state.service
    saved=service.save_feed(env['owner'],URL,stories())
    with pytest.raises(AppError) as error:
        service.admit(env['owner'],[item['id'] for item in saved['items'][:4]],'manual-remains-small')
    assert error.value.code=='INVALID_INPUT'
    assert env['db'].all('SELECT * FROM runs')==[]
