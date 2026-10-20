"""Per-page article limits replace daily canonical-feed budgets, without resets."""
import json

import pytest

from pulsefeed.config import Config
from pulsefeed.db import Database
from test_daily_limits import admin, ledger
from test_jobs import finish
from test_view_feed import source


@pytest.mark.parametrize('legacy_limit,expected', [(3,3),(10,10),(100,10)])
def test_legacy_policy_migration_preserves_accounting_and_clamps_only_page_count(env,legacy_limit,expected):
    db=env['db']
    ledger(env,env['owner'],'https://example.com/previous',2)
    db.execute("INSERT INTO quota_audit(actor,event,data,created) VALUES(?,'limits_updated','{}',11)",(env['admin'],))
    tracked=('runs','invocations','feed_views','quota_audit','settings')
    before={table:db.all('SELECT * FROM '+table) for table in tracked}
    with db.transaction() as conn:
        conn.execute('DROP TABLE quota_policy')
        conn.execute('CREATE TABLE quota_policy(id INTEGER PRIMARY KEY,global_daily_limit INTEGER NOT NULL CHECK(global_daily_limit BETWEEN 1 AND 10000),feed_daily_limit INTEGER NOT NULL CHECK(feed_daily_limit BETWEEN 1 AND 1000 AND feed_daily_limit<=global_daily_limit),reset_cutoff REAL NOT NULL,updated REAL NOT NULL,reset_epoch INTEGER NOT NULL,global_hourly_generation_limit INTEGER)')
        conn.execute('INSERT INTO quota_policy VALUES(1,100,?,41,31,3,17)',(legacy_limit,))
    migrated=Database(db.path,999,1,99)
    assert migrated.one('SELECT * FROM quota_policy')=={'id':1,'global_daily_limit':100,'feed_article_limit':expected,'reset_cutoff':41.0,'updated':31.0,'reset_epoch':3,'global_hourly_generation_limit':17}
    assert {table:migrated.all('SELECT * FROM '+table) for table in tracked}==before
    assert migrated.one('PRAGMA user_version')['user_version']==7
    Database(db.path,1,1,1)
    assert migrated.one('SELECT global_daily_limit,feed_article_limit,global_hourly_generation_limit FROM quota_policy')=={'global_daily_limit':100,'feed_article_limit':expected,'global_hourly_generation_limit':17}


def test_page_ten_is_independent_of_global_five_and_old_api_is_rejected(env):
    client=env['client'];headers=admin(env)
    rejected=client.post('/api/admin/limits',json={'global_daily_limit':100,'feed_daily_limit':10},headers=headers)
    assert rejected.status_code==400 and rejected.json()['error']['code']=='INVALID_LIMITS'
    response=client.post('/api/admin/limits',json={'global_daily_limit':5,'feed_article_limit':10},headers=headers)
    assert response.status_code==200
    assert response.json()['quotas']['page']=={'article_limit':10,'max_articles':10}
    assert 'per_feed' not in response.json()['quotas']
    service=env['app'].state.service
    data=service.view_feed(env['owner'],'demo','global-five-page-ten',source())
    assert len(data['runs'])==2
    assert service.quota_summary()['global']['reserved']==4


async def test_page_policy_changes_only_new_pages_and_same_url_does_not_shrink(env):
    service=env['app'].state.service
    fetched=source(tuple('story-'+str(i) for i in range(10)))
    service.set_limits(env['admin'],{'global_daily_limit':100,'feed_article_limit':3})
    old=service.view_feed(env['owner'],'https://example.com/rss','page-three-old',fetched)
    assert len(old['runs'])==3
    await finish(env)
    service.set_limits(env['admin'],{'global_daily_limit':100,'feed_article_limit':10})
    new=service.view_feed(env['other'],'https://example.com/rss','page-ten-new',fetched)
    assert len(new['runs'])==10  # Same canonical URL already used three attempts.
    assert len(service.reader_view(env['owner'],old['view']['id'])['runs'])==3
    assert len(service.cached_view(env['owner'],'page-three-old','https://example.com/rss')['runs'])==3
    assert len(service.cached_view(env['other'],'page-ten-new','https://example.com/rss')['runs'])==10
    assert service.quota_summary()['global']['charged']==23
    assert len(env['db'].all('SELECT * FROM runs'))==13
    audits=env['db'].all('SELECT event,data FROM quota_audit')
    assert [row['event'] for row in audits]==['limits_updated','limits_updated']
    assert json.loads(audits[-1]['data'])['current']['feed_article_limit']==10


@pytest.mark.parametrize('limit', [0,11,True,1.5])
def test_page_seed_bounds_rejected_without_changing_defaults(tmp_path,limit):
    with pytest.raises(ValueError):Database(str(tmp_path/'bad.db'),100,limit)
    config=Config(session_secret='x'*40,fake_mode=True,feed_article_limit=limit)
    with pytest.raises(ValueError,match='FEED_ARTICLE_LIMIT'):config.validate()


def test_page_environment_is_a_seed_and_legacy_daily_environment_is_not_used(monkeypatch,tmp_path):
    monkeypatch.setenv('SESSION_SECRET','x'*40)
    monkeypatch.setenv('PULSEFEED_FAKE_MODE','true')
    monkeypatch.setenv('FEED_ARTICLE_LIMIT','7')
    monkeypatch.setenv('FEED_DAILY_IMAGE_LIMIT','100')
    config=Config.from_env()
    assert config.feed_article_limit==7
    db=Database(str(tmp_path/'seed.db'),config.global_daily_limit,config.feed_article_limit)
    Database(db.path,1,2)
    assert db.one('SELECT feed_article_limit,global_daily_limit FROM quota_policy')=={'feed_article_limit':7,'global_daily_limit':100}
