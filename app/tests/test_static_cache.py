from hashlib import sha256
from pathlib import Path

import pytest

from pulsefeed.assets import asset_url
from pulsefeed.auth import issue_link
from test_jobs import finish
from test_view_feed import source


STATIC = Path(__file__).parents[1]/'pulsefeed'/'static'
IMMUTABLE = 'public, max-age=31536000, immutable'
REVALIDATE = 'public, max-age=0, must-revalidate'


@pytest.mark.parametrize('name',['app.css','app.js','Silkscreen-Regular.ttf'])
def test_correct_content_hash_public_assets_are_immutable_without_auth(env,name):
    expected=sha256((STATIC/name).read_bytes()).hexdigest()[:16]
    assert asset_url(name)==f'/static/{name}?v={expected}'
    env['client'].cookies.clear()
    response=env['client'].get(f'/static/{name}?v={expected}')
    assert response.status_code==200
    assert response.headers['cache-control']==IMMUTABLE
    assert response.content==(STATIC/name).read_bytes()


@pytest.mark.parametrize('query',['','?v=old','?v='])
def test_unversioned_and_wrong_version_revalidate(env,query):
    response=env['client'].get('/static/app.css'+query)
    assert response.status_code==200
    assert response.headers['cache-control']==REVALIDATE


@pytest.mark.parametrize('versioned',[True,False])
def test_conditional_304_keeps_correct_cache_policy(env,versioned):
    url=asset_url('app.js') if versioned else '/static/app.js?v=old'
    first=env['client'].get(url)
    response=env['client'].get(url,headers={'If-None-Match':first.headers['etag']})
    assert response.status_code==304 and not response.content
    assert response.headers['cache-control']==(IMMUTABLE if versioned else REVALIDATE)


@pytest.mark.parametrize('path',['/static/missing.js?v=1234567890abcdef','/static/%2e%2e/config.py','/missing'])
def test_missing_or_outside_static_is_never_cacheable(env,path):
    response=env['client'].get(path)
    assert response.status_code==404
    assert response.headers['cache-control']=='no-store'


@pytest.mark.parametrize(('path','status'),[
    ('/',200),('/?v=correct-looking-hash',200),('/api/session',200),
    ('/api/runs',404),('/admin',200),('/api/admin/usage',403),
    ('/access',200),('/access?token=invalid',401),
])
def test_protected_html_api_and_auth_remain_no_store(env,path,status):
    response=env['client'].get(path)
    assert response.status_code==status
    assert response.headers['cache-control']=='no-store'


def test_unauthenticated_api_and_access_redirect_no_store(env):
    env['client'].cookies.clear()
    denied=env['client'].get('/api/session')
    assert denied.status_code==401 and denied.headers['cache-control']=='no-store'
    link=issue_link(env['db'],env['config'],env['other']).replace(env['config'].base_url,'')
    response=env['client'].get(link,follow_redirects=False)
    assert response.status_code==303 and response.headers['cache-control']=='no-store'


async def test_admin_reader_renders_metrics_and_hashed_assets_before_js(env):
    service=env['app'].state.service
    viewed=service.view_feed(env['admin'],'demo','static-ssr-admin',source(('story',)))
    await finish(env)
    link=issue_link(env['db'],env['config'],env['admin']).replace(env['config'].base_url,'')
    env['client'].get(link)
    response=env['client'].get(viewed['view']['url']+'?admin')
    html=response.text.split('<script id="bootstrap"')[0]
    assert response.status_code==200 and response.headers['cache-control']=='no-store'
    assert '<dt>Input tokens</dt><dd>Unavailable</dd>' in html
    assert '<dt>Images produced</dt><dd>1</dd>' in html
    assert '<dt>Inference retries</dt><dd>0</dd>' in html
    assert '<dt>Provider queue</dt>' in html and '<dt>Runtime</dt>' in html
    for name in ('app.css','theme.js','Silkscreen-Regular.ttf'):
        assert asset_url(name) in html
    assert asset_url('app.js') in response.text
