from dataclasses import replace
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from pulsefeed.auth import seed_account
from pulsefeed.config import Config
from pulsefeed.db import Database
from pulsefeed.errors import AppError
from pulsefeed.main import create_app
from pulsefeed.provider import LocalHTTPAdapter, ReadUnavailable, SubmissionUnknown
from pulsefeed.service import Service
from pulsefeed.usage import aggregate_usage
from conftest import start
from test_view_feed import source


def local_config(**overrides):
    return replace(Config(session_secret='s'*40,inference_provider='local_http',local_worker_token='t'*40,cookie_secure=False),**overrides)


def test_local_config_no_runpod_credentials_and_explicit_env(monkeypatch):
    for name in ('RUNPOD_API_KEY','RUNPOD_QWEN_ENDPOINT_ID','RUNPOD_FLUX_ENDPOINT_ID','PULSEFEED_FAKE_MODE'):
        monkeypatch.delenv(name,raising=False)
    monkeypatch.setenv('INFERENCE_PROVIDER','local_http')
    monkeypatch.setenv('LOCAL_WORKER_TOKEN','t'*40)
    monkeypatch.setenv('SESSION_SECRET','s'*40)
    monkeypatch.setenv('APP_BASE_URL','http://127.0.0.1:8000')
    monkeypatch.setenv('COOKIE_SECURE','false')
    config=Config.from_env()
    assert config.inference_provider=='local_http' and not config.fake_mode
    assert (config.local_qwen_url,config.local_flux_url)==('http://127.0.0.1:8181','http://127.0.0.1:8182')
    assert not config.runpod_key


@pytest.mark.parametrize('url',['http://127.0.0.1:8181','http://[::1]:8181','https://localhost:8181'])
def test_loopback_local_origins_allowed(url):
    local_config(local_qwen_url=url,base_url='http://[::1]:8000').validate()


@pytest.mark.parametrize('url',['http://example.com:8181','http://0.0.0.0:8181','http://127.0.0.1.evil.test:8181','http://[::ffff:192.0.2.1]:8181','http://127.0.0.1:8181/path','http://name:pass@127.0.0.1:8181','http://127.0.0.1:8181?token=x','http://127.0.0.1:8181#fragment','file:///tmp/worker','http://[','http://127.0.0.1:99999'])
def test_local_origins_reject_nonloopback_or_ambiguous_routes(url):
    with pytest.raises(ValueError,match='loopback'):
        local_config(local_qwen_url=url).validate()


@pytest.mark.parametrize('overrides',[
    {'inference_provider':'other'}, {'local_worker_token':'short'},
    {'local_worker_token':'t'*32+'\n'}, {'base_url':'http://example.com'},
    {'base_url':'http://127.0.0.1.evil.test'}, {'base_url':'http://name@localhost'},
])
def test_local_invalid_config_and_cookie_guard(overrides):
    with pytest.raises(ValueError):local_config(**overrides).validate()


def test_default_live_runpod_still_requires_credentials_and_secure_cookie():
    with pytest.raises(ValueError,match='Runpod'):Config(session_secret='s'*40).validate()
    with pytest.raises(ValueError,match='COOKIE_SECURE'):
        Config(session_secret='s'*40,runpod_key='key',qwen_endpoint='q',flux_endpoint='f',cookie_secure=False).validate()


async def test_local_adapter_native_routes_bearer_and_wrapper():
    requests=[]
    def transport(request):
        requests.append(request)
        assert request.headers['Authorization']=='Bearer '+'t'*40
        if request.method=='POST' and request.url.path=='/run':
            assert json.loads(request.content)=={'input':{'schema_version':1}}
            return httpx.Response(200,json={'id':'job/id','status':'IN_QUEUE'})
        if request.method=='GET':
            return httpx.Response(200,json={'status':'COMPLETED','output':{'result':{'ok':True,'error':None}}})
        return httpx.Response(200,json={'status':'IN_PROGRESS','cancel_requested':True})
    adapter=LocalHTTPAdapter(local_config(),httpx.AsyncClient(transport=httpx.MockTransport(transport)))
    try:
        assert await adapter.submit('qwen',{'schema_version':1})=='job/id'
        status=await adapter.status('flux','job/id')
        assert status['output']=={'result':{'ok':True,'error':None}}
        assert (await adapter.cancel('flux','job/id'))['status']=='IN_PROGRESS'
        assert [(r.method,r.url.port,r.url.raw_path) for r in requests]==[
            ('POST',8181,b'/run'),('GET',8182,b'/status/job%2Fid'),('POST',8182,b'/cancel/job%2Fid')]
    finally:await adapter.close()


@pytest.mark.parametrize('status',[500,503,307])
async def test_local_submission_ambiguous_never_resubmits(status):
    calls=[]
    def transport(request):
        calls.append(request)
        return httpx.Response(status,json={'status':'IN_QUEUE'},headers={'Location':'http://example.com/'})
    adapter=LocalHTTPAdapter(local_config(),httpx.AsyncClient(transport=httpx.MockTransport(transport),follow_redirects=False))
    try:
        with pytest.raises(SubmissionUnknown):await adapter.submit('flux',{'test':True})
        assert len(calls)==1
    finally:await adapter.close()


async def test_local_auth_rejected_status_retry_and_missing():
    replies=[401,503,200,404];calls=[];delays=[]
    def transport(request):
        calls.append(request)
        return httpx.Response(replies.pop(0),json={'id':'job','status':'IN_PROGRESS'},headers={'Retry-After':'0'})
    async def sleep(delay):delays.append(delay)
    adapter=LocalHTTPAdapter(local_config(),httpx.AsyncClient(transport=httpx.MockTransport(transport)),sleep)
    try:
        with pytest.raises(AppError,match='rejected'):await adapter.submit('qwen',{})
        assert (await adapter.status('qwen','job'))['status']=='IN_PROGRESS'
        assert await adapter.status('qwen','missing')=={'status':'MISSING'}
        assert len(calls)==4 and delays==[0]
    finally:await adapter.close()


def test_provider_profile_stable_rotation_rejects_switch_and_never_persists_secrets(tmp_path):
    config=local_config(database_path=str(tmp_path/'bound.db'))
    db=Database(config.database_path);db.bind_provider(config.provider_profile())
    db.bind_provider(replace(config,local_worker_token='rotated-'+'x'*40).provider_profile())
    assert set(db.all('SELECT key,value FROM app_metadata')[0])=={'key','value'}
    metadata=json.dumps(db.all('SELECT * FROM app_metadata'))
    assert config.local_worker_token not in metadata and config.local_qwen_url not in metadata
    for changed in (replace(config,local_flux_url='http://127.0.0.1:8183'),replace(config,fake_mode=True),replace(config,inference_provider='runpod',cookie_secure=True,runpod_key='key',qwen_endpoint='q',flux_endpoint='f')):
        with pytest.raises(ValueError,match='provider/endpoints'):create_app(changed)


def test_create_app_selects_real_local_adapter_and_token_rotation(tmp_path):
    config=local_config(database_path=str(tmp_path/'app.db'))
    for token in ('t'*40,'r'*40):
        app=create_app(replace(config,local_worker_token=token,start_coordinator=False))
        with TestClient(app) as client:
            assert isinstance(app.state.provider,LocalHTTPAdapter)
            assert client.get('/health').json()['mode']=='live'


async def test_legacy_synthetic_identity_upgrade_but_unknown_or_live_refused(env):
    start(env)
    await env['app'].state.coordinator.tick()
    db=env['db']
    assert db.one('SELECT execution_mode FROM invocations')['execution_mode']=='synthetic'
    db.execute("DELETE FROM app_metadata WHERE key='provider_profile'")
    db.bind_provider(env['config'].provider_profile(),synthetic=True)
    for mode in (None,'live'):
        db.execute("DELETE FROM app_metadata WHERE key='provider_profile'")
        db.execute('UPDATE invocations SET execution_mode=?',(mode,))
        with pytest.raises(ValueError,match='no verified provider identity'):
            db.bind_provider(env['config'].provider_profile(),synthetic=True)


def test_read_connections_close_on_success_exception_and_snapshot(tmp_path,monkeypatch):
    connections=[];original=sqlite3.connect
    class Tracked(sqlite3.Connection):
        closed=False
        def close(self):
            self.closed=True
            super().close()
    def connect(*args,**kwargs):
        connection=original(*args,**kwargs,factory=Tracked)
        connections.append(connection)
        return connection
    monkeypatch.setattr(sqlite3,'connect',connect)
    db=Database(str(tmp_path/'connections.db'))
    owner=seed_account(db,'reader','evaluator')
    service=Service(db,local_config())
    feed=service.save_feed(owner,'https://example.com/rss',source())
    view=service.view_feed(owner,'https://example.com/rss','connection-view-001',source())
    service.reader_view(owner,view['view']['id']);service.quota_summary();db.settings();aggregate_usage(db)
    with pytest.raises(sqlite3.OperationalError):db.one('SELECT nonexistent FROM feeds')
    with pytest.raises(AppError):service.feed(owner,'foreign')
    assert connections and all(connection.closed for connection in connections)
