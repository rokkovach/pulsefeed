"""Explicit reset clears spent generation allowances without deleting history."""
import json
import time

import pytest

from pulsefeed.db import Database
from pulsefeed.errors import AppError
from pulsefeed.service import Service
from pulsefeed.usage import aggregate_usage
from test_daily_limits import admin, ledger, settle
from test_jobs import finish
from test_view_feed import source


def owner_slots(env):
    with env['db'].transaction() as conn:
        return env['app'].state.service._admission_budget(conn,env['owner'],time.time())['owner_slots']


def test_thirty_exhausted_starts_and_same_visitor_attempts_reset_then_admit_without_clock_advance(env,monkeypatch):
    now=1792065600.0
    monkeypatch.setattr(time,'time',lambda:now)
    service=env['app'].state.service
    for _ in range(30):ledger(env,env['owner'],'https://example.com/used',1)
    assert service.quota_summary()['hourly']['used']==30 and owner_slots(env)==0
    rejected=env['client'].post('/api/view-feed',json={'url':'demo','idempotency_key':'before-reset-blocked'},headers=env['headers'])
    assert rejected.json()['generation']['error']['code']=='QUOTA_EXCEEDED'
    tables=('runs','invocations','feeds','items','feed_views','batches','settings','rate_events')
    history={table:env['db'].all('SELECT * FROM '+table) for table in tables}
    usage=aggregate_usage(env['db'],window='all',now=now)
    policy=env['db'].one('SELECT global_daily_limit,feed_article_limit,global_hourly_generation_limit FROM quota_policy')
    result=service.reset_limits(env['admin'],{'confirm':True})
    assert result['hourly']=={'limit':30,'used':0,'remaining':30,'window_seconds':3600,'next_slot_at':None}
    assert result['global']['charged']==0 and owner_slots(env)==15
    assert {table:env['db'].all('SELECT * FROM '+table) for table in tables}==history
    assert aggregate_usage(env['db'],window='all',now=now)==usage
    assert env['db'].one('SELECT global_daily_limit,feed_article_limit,global_hourly_generation_limit FROM quota_policy')==policy
    assert env['db'].all('SELECT event FROM quota_audit')==[{'event':'allowance_reset'}]
    # This is the same visitor cookie and the same timestamp, not a new account.
    accepted=env['client'].post('/api/view-feed',json={'url':'demo','idempotency_key':'after-reset-accepted'},headers=env['headers']).json()
    assert accepted['generation']['status']=='started' and len(accepted['runs'])==3
    assert service.quota_summary()['hourly']['used']==3
    assert {row['admission_epoch'] for row in env['db'].all("SELECT admission_epoch FROM runs WHERE state='queued'")}=={1}
    assert env['provider'].submissions==[]
    # The old blocked request is immutable; reset does not replay it automatically.
    assert service.cached_view(env['owner'],'before-reset-blocked','demo')['generation']['status']=='blocked'


async def test_repeated_resets_hold_unknown_work_but_late_settlement_never_restores_hourly_starts(env,monkeypatch):
    now=1792065600.0
    monkeypatch.setattr(time,'time',lambda:now)
    service=env['app'].state.service
    env['config'].image_quota=6
    data=service.view_feed(env['owner'],'demo','held-before-reset',source(('one','two')))
    first=data['runs'][0]['id']
    env['db'].execute("UPDATE runs SET state='submission_unknown' WHERE id=?",(first,))
    for _ in range(2):
        summary=service.reset_limits(env['admin'],{'confirm':True})
        assert summary['hourly']['used']==2 and summary['global']['reserved']==4
        assert owner_slots(env)==1
    assert {row['admission_epoch'] for row in env['db'].all('SELECT admission_epoch FROM runs')}=={0}
    # Hourly starts age out, but uncertain image reservations do not.
    now+=3700
    assert service.quota_summary()['hourly']['used']==0
    assert service.quota_summary()['global']['reserved']==4 and owner_slots(env)==1
    for run in data['runs']:settle(env,run['id'],1)
    summary=service.quota_summary()
    assert summary['hourly']['used']==0 and summary['global']['used']==2 and summary['global']['reserved']==0
    assert owner_slots(env)==2  # Actual late charges remain in the visitor allowance.
    assert env['db'].all('SELECT admission_epoch,quota_epoch FROM runs')==[{'admission_epoch':0,'quota_epoch':2}]*2
    new=service.view_feed(env['owner'],'demo','fresh-after-late-settlement',source(('one',)))
    assert len(new['runs'])==1 and service.quota_summary()['hourly']['used']==1
    assert env['db'].one('SELECT admission_epoch FROM runs WHERE id=?',(new['runs'][0]['id'],))['admission_epoch']==2


async def test_recent_pre_reset_active_start_drops_when_completed_without_resurrection(env):
    service=env['app'].state.service
    data=service.view_feed(env['owner'],'demo','recent-active-reset',source(('one',)))
    assert service.reset_limits(env['admin'],{'confirm':True})['hourly']['used']==1
    await finish(env)
    summary=service.quota_summary()
    assert summary['hourly']['used']==0 and summary['global']['used']==1
    assert env['db'].one('SELECT admission_epoch,quota_epoch FROM runs')=={'admission_epoch':0,'quota_epoch':1}
    service.reset_limits(env['admin'],{'confirm':True})
    assert service.quota_summary()['hourly']['used']==0 and service.quota_summary()['global']['charged']==0
    assert env['db'].one('SELECT admission_epoch FROM runs')['admission_epoch']==0


def test_retry_after_ignores_old_terminal_start_and_tracks_current_epoch_exactly(env,monkeypatch):
    now=1792065600.0
    monkeypatch.setattr(time,'time',lambda:now)
    service=env['app'].state.service
    env['db'].execute('UPDATE quota_policy SET global_hourly_generation_limit=1')
    ledger(env,env['other'],'https://example.com/old',1,at=now-3000)
    service.reset_limits(env['admin'],{'confirm':True})
    ledger(env,env['other'],'https://example.com/new',1,at=now-100)
    summary=service.quota_summary()['hourly']
    assert summary['used']==1 and summary['next_slot_at']==now+3500
    item=service.save_feed(env['owner'],'demo',source(('one',)))['items'][0]['id']
    with pytest.raises(AppError) as error:service.admit(env['owner'],[item],'retry-after-current-epoch')
    assert error.value.code=='GLOBAL_HOURLY_LIMIT' and error.value.retry_after==3501


def test_migration_stamps_legacy_admission_epoch_once_and_restart_never_restamps(env):
    db=env['db'];service=env['app'].state.service
    ledger(env,env['owner'],'https://example.com/legacy',1)
    before={table:db.all('SELECT * FROM '+table) for table in ('invocations','settings','quota_audit')}
    with db.transaction() as conn:
        conn.execute('ALTER TABLE runs DROP COLUMN admission_epoch')
        conn.execute('UPDATE quota_policy SET reset_epoch=3')
        conn.execute('PRAGMA user_version=6')
    Database(db.path)
    assert db.one('SELECT admission_epoch FROM runs')['admission_epoch']==3
    assert service.quota_summary()['hourly']['used']==1
    assert {table:db.all('SELECT * FROM '+table) for table in before}==before
    assert db.one('PRAGMA user_version')['user_version']==7
    service.reset_limits(env['admin'],{'confirm':True})
    Database(db.path)
    assert db.one('SELECT admission_epoch FROM runs')['admission_epoch']==3
    assert service.quota_summary()['hourly']['used']==0
    restarted=Service(db,env['config'])
    new=restarted.view_feed(env['owner'],'demo','restart-new-admission',source(('new',)))
    assert db.one('SELECT admission_epoch FROM runs WHERE id=?',(new['runs'][0]['id'],))['admission_epoch']==4


def test_reset_preserves_request_throttles_and_requires_admin_csrf_confirmation(env):
    service=env['app'].state.service
    service.limit(env['owner'],'submit')
    service.limit(env['owner'],'rss')
    env['config'].submit_limit=1;env['config'].rss_limit=1
    before=env['db'].all('SELECT * FROM rate_events')
    assert env['client'].post('/api/admin/limits/reset',json={'confirm':True},headers=env['headers']).status_code==403
    headers=admin(env)
    assert env['client'].post('/api/admin/limits/reset',json={'confirm':True}).status_code==403
    assert env['client'].post('/api/admin/limits/reset',json={'confirm':False},headers=headers).status_code==400
    assert env['db'].all('SELECT * FROM quota_audit')==[]
    response=env['client'].post('/api/admin/limits/reset',json={'confirm':True},headers=headers)
    assert response.status_code==200
    assert all(row in env['db'].all('SELECT * FROM rate_events') for row in before)
    for action in ('submit','rss'):
        with pytest.raises(AppError) as error:service.limit(env['owner'],action)
        assert error.value.status==429
    event=env['db'].one('SELECT event,data FROM quota_audit')
    assert event['event']=='allowance_reset' and json.loads(event['data'])['scope']=='daily_and_hourly'
    html=env['client'].get('/admin').text
    assert 'Reset daily and hourly allowances now?' in html
    assert 'not cleared by a daily reset' not in html
