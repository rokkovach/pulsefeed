import pytest
from fastapi.testclient import TestClient
from pulsefeed.config import Config
from pulsefeed.main import create_app
from pulsefeed.auth import seed_account,issue_link
from pulsefeed.provider import FakeRunpodAdapter
from pulsefeed.rss import demo_feed


@pytest.fixture
def env(tmp_path):
    config = Config(database_path=str(tmp_path / 'test.db'),session_secret='x'*40,fake_mode=True,cookie_secure=False,start_coordinator=False,public_access_enabled=False)
    provider = FakeRunpodAdapter(); provider.delay = 0
    app = create_app(config,provider,lambda url:demo_feed())
    db = app.state.db
    owner = seed_account(db,'evaluator','evaluator')
    admin = seed_account(db,'admin','admin')
    other = seed_account(db,'other','evaluator')
    with TestClient(app) as client:
        client.get(issue_link(db,config,owner).replace(config.base_url,''))
        csrf = client.get('/api/session').json()['csrf']
        yield dict(app=app,db=db,config=config,provider=provider,client=client,owner=owner,admin=admin,other=other,headers={'X-CSRF-Token':csrf})


def add_demo(env):
    return env['app'].state.service.save_feed(env['owner'],'demo',demo_feed())['items']


def start(env,key='sample-key-0001',count=1):
    fetched=demo_feed();fetched['items']=fetched['items'][:count]
    viewed=env['app'].state.service.view_feed(env['owner'],'demo',key,fetched)
    assert viewed['generation']['status']=='started',viewed
    env['view_id']=viewed['view']['id'];env['view_data']=viewed
    return viewed['generation']['batch']
