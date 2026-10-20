from dataclasses import replace
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from pulsefeed.auth import issue_link, seed_account
from pulsefeed.config import Config
from pulsefeed.main import create_app
from pulsefeed.provider import FakeRunpodAdapter
from pulsefeed.rss import demo_feed
from test_view_feed import source


ADMIN_TOKEN = 'RuntimeAdmin_TestSecret_0123456789_ABCDEFGHIJKLMNOPQRSTUVWXYZ'
ROTATED_TOKEN = 'RotatedRuntime_TestSecret_9876543210_abcdefghijklmnopqrstuvwxyz'


@pytest.fixture
def public(tmp_path):
    config=Config(database_path=str(tmp_path/'public.db'),session_secret='s'*40,fake_mode=True,cookie_secure=False,start_coordinator=False,
                  public_access_enabled=True,admin_access_token=ADMIN_TOKEN,base_url='http://127.0.0.1:8000')
    provider=FakeRunpodAdapter();provider.delay=0
    fetches=[]
    async def fetch(url):
        fetches.append(url)
        return demo_feed()
    app=create_app(config,provider,fetch)
    with TestClient(app) as client:
        yield {'app':app,'config':config,'provider':provider,'db':app.state.db,'client':client,'fetches':fetches}


def bootstrap(client):
    response=client.get('/')
    assert response.status_code==200
    return client.get('/api/session').json()


def grant(public,client=None,**kwargs):
    client=client or public['client']
    return client.get('/',params=[(public['config'].admin_access_param,public['config'].admin_access_token)],follow_redirects=False,**kwargs)


def test_public_home_bootstraps_once_but_apis_do_not(public):
    client=public['client'];db=public['db']
    for path in ('/api/session','/api/admin/usage'):
        assert client.get(path).status_code==401
    assert client.post('/api/view-feed',json={'url':'demo','idempotency_key':'no-session-yet'}).status_code==401
    assert db.all('SELECT * FROM accounts')==[]
    home=client.get('/')
    assert home.status_code==200 and home.context['public_access'] is True
    assert home.context['account']['role']=='evaluator' and home.context['view'] is None
    assert 'HttpOnly' in home.headers['set-cookie'] and 'SameSite=lax' in home.headers['set-cookie']
    before={table:db.all('SELECT * FROM '+table) for table in ('accounts','sessions','rate_events','runs','invocations')}
    for _ in range(2):
        response=client.get('/')
        assert response.status_code==200 and 'set-cookie' not in response.headers
    assert before=={table:db.all('SELECT * FROM '+table) for table in before}
    assert public['provider'].submissions==[]


def test_public_cookie_jars_isolate_owners_and_csrf(public):
    first=bootstrap(public['client'])
    with TestClient(public['app']) as second:
        other=bootstrap(second)
        assert first['account']['id']!=other['account']['id']
        assert first['csrf']!=other['csrf']
        denied=public['client'].post('/api/view-feed',json={'url':'demo','idempotency_key':'public-csrf-key'})
        assert denied.status_code==403
        data=public['client'].post('/api/view-feed',json={'url':'demo','idempotency_key':'public-csrf-key'},headers={'X-CSRF-Token':first['csrf']}).json()
        assert data['view']['expires_at']==data['view']['created_at']+3600
        assert second.get(data['view']['url']).status_code==404
        for path in ('/api/batches/'+data['generation']['batch']['id'],'/api/runs/'+data['runs'][0]['id']+'/image'):
            response=second.get(path+'?view_id='+data['view']['id'])
            assert response.status_code==404 and response.json()['error']['code']=='VIEW_NOT_FOUND'
        assert public['app'].state.service.quota_summary()['hourly']['used']==3
        assert public['provider'].submissions==[]


def test_no_visitor_is_minted_for_foreign_missing_or_admin_html(public):
    client=public['client'];db=public['db']
    for path in ('/feeds/missing','/?feed=legacy'):
        response=client.get(path)
        assert response.status_code==404 and response.context['account'] is None
        assert response.context['public_access'] is True and 'set-cookie' not in response.headers
    locked=client.get('/admin')
    assert locked.status_code==200 and locked.headers['content-type'].startswith('text/html')
    assert 'type="password"' in locked.text
    assert 'Back to home' in locked.text
    assert client.get('/?admin').status_code==401
    assert db.all('SELECT * FROM accounts')==[] and db.all('SELECT * FROM sessions')==[]


def test_invalid_cookie_bootstraps_new_owner_only_on_home(public):
    first=bootstrap(public['client'])
    cookie=next(cookie for cookie in public['client'].cookies.jar if cookie.name=='pulsefeed_session')
    public['client'].cookies.set('pulsefeed_session','invalid-session',domain=cookie.domain,path=cookie.path)
    assert public['client'].get('/api/session').status_code==401
    assert public['client'].get('/feeds/missing').status_code==404
    public['client'].get('/')
    second=public['client'].get('/api/session').json()
    assert second['account']['id']!=first['account']['id']
    assert len(public['db'].all('SELECT * FROM accounts'))==2


def test_bootstrap_throttles_before_account_growth(public):
    public['config'].visitor_limit=2
    for _ in range(2):
        public['client'].cookies.clear()
        assert public['client'].get('/').status_code==200
    public['client'].cookies.clear()
    response=public['client'].get('/')
    assert response.status_code==429 and response.headers['retry-after']
    assert len(public['db'].all('SELECT * FROM accounts'))==2
    assert len(public['db'].all("SELECT * FROM rate_events WHERE action='visitor_bootstrap'"))==2


def test_public_rss_ip_gate_survives_new_cookie_and_replay_is_free(public):
    public['config'].rss_limit=1
    first=bootstrap(public['client'])
    body={'url':'https://example.com/rss','idempotency_key':'public-ip-first-key'}
    response=public['client'].post('/api/view-feed',json=body,headers={'X-CSRF-Token':first['csrf']})
    assert response.status_code==200
    replay=public['client'].post('/api/view-feed',json=body,headers={'X-CSRF-Token':first['csrf']})
    assert replay.status_code==200 and replay.json()['view']==response.json()['view']
    public['client'].cookies.clear()
    second=bootstrap(public['client'])
    denied=public['client'].post('/api/view-feed',json=dict(body,idempotency_key='public-ip-second-key'),headers={'X-CSRF-Token':second['csrf']})
    assert denied.status_code==429 and denied.headers['retry-after']
    assert len(public['fetches'])==1
    assert len(public['db'].all('SELECT * FROM feed_views'))==1
    assert len(public['db'].all("SELECT * FROM rate_events WHERE action='public_rss'"))==1


def test_elevation_preserves_view_owner_rotates_cookie_csrf_and_never_promotes_account(public):
    client=public['client'];visitor=bootstrap(client)
    data=client.post('/api/view-feed',json={'url':'demo','idempotency_key':'elevated-view-key'},headers={'X-CSRF-Token':visitor['csrf']}).json()
    before=public['app'].state.service.quota_summary()
    old_cookie=client.cookies.get('pulsefeed_session')
    response=grant(public)
    assert response.status_code==303 and response.headers['location']=='/admin'
    assert 'HttpOnly' in response.headers['set-cookie'] and 'Max-Age=3600' in response.headers['set-cookie']
    current=client.get('/api/session').json()
    assert current['account']['id']==visitor['account']['id'] and current['account']['role']=='admin'
    assert current['csrf']!=visitor['csrf'] and client.cookies.get('pulsefeed_session')!=old_cookie
    assert public['db'].one('SELECT role FROM accounts')['role']=='evaluator'
    assert client.get(data['view']['url']).status_code==200 and client.get('/admin').status_code==200
    with TestClient(public['app']) as old:
        old.cookies.set('pulsefeed_session',old_cookie)
        assert old.get('/api/session').status_code==401
    denied=client.post('/api/admin/limits/reset',json={'confirm':True},headers={'X-CSRF-Token':visitor['csrf']})
    assert denied.status_code==403
    after=public['app'].state.service.quota_summary()
    assert before['global']==after['global'] and before['hourly']==after['hourly']
    assert public['provider'].submissions==[]


def test_new_admin_browsers_have_distinct_visitor_owners(public):
    assert grant(public).status_code==303
    first=public['client'].get('/api/session').json()
    with TestClient(public['app']) as second:
        assert grant(public,second).status_code==303
        other=second.get('/api/session').json()
        assert first['account']['role']==other['account']['role']=='admin'
        assert first['account']['id']!=other['account']['id']
    assert all(row['role']=='evaluator' for row in public['db'].all('SELECT role FROM accounts'))


@pytest.mark.parametrize('values',[['wrong-secret'],[ADMIN_TOKEN,ADMIN_TOKEN],['A'*1000],['snowman_☃'],['']])
def test_bad_duplicate_oversize_unicode_admin_credentials_never_mint(public,values):
    response=public['client'].get('/',params=[('admin_key',value) for value in values],follow_redirects=False)
    assert response.status_code==303 and response.headers['location']=='/admin'
    assert 'set-cookie' not in response.headers
    assert public['db'].all('SELECT * FROM accounts')==[]
    assert len(public['db'].all("SELECT * FROM rate_events WHERE action='admin_exchange'"))==1
    assert ADMIN_TOKEN not in response.text+str(response.headers)


def test_admin_throttle_commits_bad_attempt_then_rejects_valid_grant(public):
    public['config'].exchange_limit=1
    first=public['client'].get('/?admin_key=wrong',follow_redirects=False)
    assert first.status_code==303
    blocked=grant(public)
    assert blocked.status_code==303 and blocked.headers['retry-after']
    assert 'set-cookie' not in blocked.headers and public['db'].all('SELECT * FROM accounts')==[]
    assert len(public['db'].all("SELECT * FROM rate_events WHERE action='admin_exchange'"))==1


@pytest.mark.parametrize('change',['token','param','disable'])
def test_admin_capability_rotation_revokes_irreversibly_on_authenticate(public,change):
    grant(public)
    cfg=public['config'];original=(cfg.admin_access_param,cfg.admin_access_token)
    if change=='token':cfg.admin_access_token=ROTATED_TOKEN
    elif change=='param':cfg.admin_access_param='new_admin_gate'
    else:cfg.admin_access_token=''
    assert public['client'].get('/api/session').status_code==401
    assert public['db'].one('SELECT revoked FROM sessions')['revoked']==1
    cfg.admin_access_param,cfg.admin_access_token=original
    assert public['client'].get('/api/session').status_code==401


def test_restart_revokes_stale_caps_but_matching_restart_preserves_them(public):
    grant(public);cookie=public['client'].cookies.get('pulsefeed_session')
    same=create_app(public['config'],FakeRunpodAdapter())
    with TestClient(same) as client:
        client.cookies.set('pulsefeed_session',cookie)
        assert client.get('/api/session').json()['account']['role']=='admin'
    create_app(replace(public['config'],admin_access_token=ROTATED_TOKEN),FakeRunpodAdapter())
    # Restoring the prior configuration must not resurrect a revoked capability.
    create_app(public['config'],FakeRunpodAdapter())
    assert public['client'].get('/api/session').status_code==401


def test_disabling_token_on_restart_revokes_even_in_private_mode(public):
    grant(public)
    create_app(replace(public['config'],public_access_enabled=False,admin_access_token=''),FakeRunpodAdapter())
    create_app(public['config'],FakeRunpodAdapter())
    assert public['client'].get('/api/session').status_code==401


def test_grant_secret_never_appears_in_html_headers_session_or_storage(public):
    response=grant(public)
    dashboard=public['client'].get('/admin')
    session=public['client'].get('/api/session')
    records=str(public['db'].all('SELECT * FROM sessions'))+str(public['db'].all('SELECT * FROM accounts'))
    for content in (response.text+str(response.headers),dashboard.text,session.text,records,repr(public['config'])):
        assert ADMIN_TOKEN not in content and public['config'].session_secret not in content
    assert public['config'].admin_access_param not in dashboard.text+session.text
    assert 'admin_capability' not in session.text


def test_legacy_admin_link_and_session_cannot_bypass_runtime_policy(public):
    db=public['db'];cfg=public['config']
    admin=seed_account(db,'legacy-admin','admin')
    legacy_cfg=replace(cfg,public_access_enabled=False,admin_access_token='')
    from pulsefeed.auth import exchange
    link=issue_link(db,legacy_cfg,admin)
    raw=exchange(db,legacy_cfg,link.split('token=')[1],'legacy-fixture-ip')
    public['client'].cookies.set('pulsefeed_session',raw)
    assert public['client'].get('/api/session').json()['account']['role']=='evaluator'
    assert public['client'].get('/api/admin/usage').status_code==403
    new_link=issue_link(db,cfg,admin).replace(cfg.base_url,'')
    assert public['client'].get(new_link,follow_redirects=False).status_code==403
    # Explicit private mode retains legacy behavior only without a runtime token.
    private=create_app(legacy_cfg,FakeRunpodAdapter())
    with TestClient(private) as client:
        client.cookies.set('pulsefeed_session',raw)
        assert client.get('/api/session').json()['account']['role']=='admin'
    private_with_token=create_app(replace(cfg,public_access_enabled=False),FakeRunpodAdapter())
    with TestClient(private_with_token) as client:
        client.cookies.set('pulsefeed_session',raw)
        assert client.get('/api/admin/usage').status_code==403


def test_admin_session_lifetime_is_independent_and_expires(public,monkeypatch):
    public['config'].admin_session_seconds=60
    grant(public)
    row=public['db'].one('SELECT * FROM sessions')
    assert row['expires']<=__import__('time').time()+60
    monkeypatch.setattr('pulsefeed.auth.time.time',lambda:row['expires'])
    assert public['client'].get('/api/session').status_code==401


@pytest.mark.parametrize('param',['admin','feed','token','window','period','view_id','v','next','bad-key','bad key','☃','9key','a'*65])
def test_admin_param_rejects_reserved_or_nonidentifier(public,param):
    with pytest.raises(ValueError):replace(public['config'],admin_access_param=param).validate()


@pytest.mark.parametrize('token',['short','A'*40,'x'*257,'Snowman_☃'+'a'*40,'has whitespace_'+'a'*40,'s'*40])
def test_admin_token_rejects_unsafe_weak_or_session_secret(public,token):
    with pytest.raises(ValueError) as error:replace(public['config'],admin_access_token=token).validate()
    assert token not in str(error.value)


@pytest.mark.parametrize('url',['http://production.example','https://user:pass@production.example','https://production.example/path','https://production.example?token=x','https://production.example#fragment','https://production.example\n','http://['])
def test_production_origin_validation_rejects_unsafe_bases(public,url):
    with pytest.raises(ValueError):replace(public['config'],base_url=url,cookie_secure=True).validate()


def test_production_https_secure_cookie_and_local_exceptions(public):
    replace(public['config'],base_url='https://production.example',cookie_secure=True).validate()
    with pytest.raises(ValueError):replace(public['config'],base_url='https://production.example',cookie_secure=False).validate()
    replace(public['config'],base_url='http://localhost:8000',cookie_secure=False).validate()
    for seconds in (0,-1,True,86401):
        with pytest.raises(ValueError):replace(public['config'],admin_session_seconds=seconds).validate()


def test_custom_parameter_from_env_and_default_public_mode(public):
    values={'SESSION_SECRET':'s'*40,'PULSEFEED_FAKE_MODE':'true','COOKIE_SECURE':'false','APP_BASE_URL':'http://127.0.0.1:8000','ADMIN_ACCESS_PARAM':'operator_gate','ADMIN_ACCESS_TOKEN':ADMIN_TOKEN}
    with patch.dict(os.environ,values,clear=True):
        config=Config.from_env()
    assert config.public_access_enabled and config.admin_access_param=='operator_gate' and config.admin_session_seconds==3600 and config.visitor_limit==20
    public['config'].admin_access_param='operator_gate'
    assert grant(public).status_code==303
    assert public['client'].get('/api/session').json()['account']['role']=='admin'
