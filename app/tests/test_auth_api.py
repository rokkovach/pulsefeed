import time
from pulsefeed.auth import issue_link,token_hash
from pulsefeed.db import DEFAULT_SETTINGS
from conftest import add_demo,start


def test_access_link_single_use_expiry_and_clean_redirect(env):
    client=env['client']; db=env['db'];config=env['config']
    url=issue_link(db,config,env['other']).replace(config.base_url,'')
    response=client.get(url,follow_redirects=False)
    assert response.status_code==303 and response.headers['location']=='/'
    cookie=response.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'SameSite=lax' in cookie
    assert 'no-referrer'==response.headers['referrer-policy']
    assert client.get(url).status_code==401
    expired=issue_link(db,config,env['other']).replace(config.base_url,'')
    db.execute('UPDATE access_tokens SET expires=0 WHERE used IS NULL')
    assert client.get(expired).status_code==401
    assert all(len(row['hash'])==64 for row in db.all('SELECT hash FROM access_tokens'))


def test_revoked_and_expired_session(env):
    env['db'].execute('UPDATE sessions SET expires=0')
    assert env['client'].get('/api/runs').status_code==401
    assert env['client'].get('/').status_code==401


def test_revoked_link(env):
    link=issue_link(env['db'],env['config'],env['other']).replace(env['config'].base_url,'')
    env['db'].execute('UPDATE access_tokens SET revoked=1')
    assert env['client'].get(link).status_code==401


def test_csrf_admin_and_ownership(env):
    client=env['client']
    assert client.post('/api/view-feed',json={'url':'demo','idempotency_key':'csrf-view-key'}).status_code==403
    assert client.post('/api/settings',json=DEFAULT_SETTINGS,headers=env['headers']).status_code==403
    assert client.get('/api/feeds/not-mine/items').status_code==404
    assert client.get('/api/batches/not-mine').status_code==404
    assert client.get('/api/runs/not-mine/image').status_code==404
    batch=start(env)
    link=issue_link(env['db'],env['config'],env['other']).replace(env['config'].base_url,'')
    client.get(link)
    assert client.get('/api/batches/'+batch['id']+'?view_id='+env['view_id']).status_code==404
    csrf=client.get('/api/session').json()['csrf']
    assert client.post('/api/runs/'+batch['runs'][0]['id']+'/retry?view_id='+env['view_id'],json={'idempotency_key':'another-key'},headers={'X-CSRF-Token':csrf}).status_code==404


def test_admin_form_snapshot_validation(env):
    client=env['client']
    client.get(issue_link(env['db'],env['config'],env['admin']).replace(env['config'].base_url,''))
    headers={'X-CSRF-Token':client.get('/api/session').json()['csrf']}
    settings=dict(DEFAULT_SETTINGS,blocked_terms='restrained');settings.pop('flux_template_version')
    assert client.post('/api/settings',json=settings,headers=headers).status_code==400
    settings=dict(DEFAULT_SETTINGS,flux_prefix='Watercolor art {{qwen_prompt}}');settings.pop('flux_template_version')
    assert client.post('/api/settings',json=settings,headers=headers).status_code==200
    assert env['db'].settings()['flux_prefix']=='Watercolor art {{qwen_prompt}}'


def test_logout_and_exchange_limit(env):
    client=env['client'];config=env['config'];config.exchange_limit=2
    assert client.post('/logout',headers=env['headers']).status_code==200
    assert client.get('/api/session').status_code==401
    responses=[client.get('/access?token=invalid') for _ in range(3)]
    assert responses[-1].status_code==429 and 'retry-after' in responses[-1].headers


def test_rss_rate_before_fetch(env):
    config=env['config'];config.rss_limit=1
    assert env['client'].post('/api/view-feed',json={'url':'demo','idempotency_key':'rss-first-key'},headers=env['headers']).status_code==200
    response=env['client'].post('/api/view-feed',json={'url':'demo','idempotency_key':'rss-second-key'},headers=env['headers'])
    assert response.status_code==429 and response.headers['retry-after']
    assert env['provider'].submissions==[]


def test_input_and_feed_boundaries(env):
    items=add_demo(env)
    for chosen in ([],[items[0]['id']]*2,[x['id'] for x in items]+['unknown']):
        from pulsefeed.errors import AppError
        import pytest
        with pytest.raises(AppError) as error:
            env['app'].state.service.admit(env['owner'],chosen,'valid-key-1')
        assert error.value.status==400
    assert env['provider'].submissions==[]
    assert env['client'].post('/api/feeds',json={'url':'https://example.com/new'},headers=env['headers']).status_code==404
    assert env['client'].post('/api/settings',content='x'*70000,headers=env['headers']).status_code==403


def test_form_logout_bounds_and_csrf(env):
    client=env['client']
    assert client.post('/logout',data={'csrf':'wrong'},follow_redirects=False).status_code==403
    assert client.post('/logout',content='x'*2050,follow_redirects=False).status_code==413
    assert client.post('/logout',data={'csrf':env['headers']['X-CSRF-Token']},follow_redirects=False).status_code==303
    assert client.get('/api/session').status_code==401


def test_forwarded_ip_only_from_trusted_peer(env):
    from starlette.requests import Request
    from pulsefeed.auth import client_ip
    scope={'type':'http','client':('192.0.2.10',123),'headers':[(b'x-forwarded-for',b'8.8.8.8, 192.0.2.11')]}
    request=Request(scope)
    assert client_ip(request,env['config'])=='192.0.2.10'
    env['config'].trusted_proxies=('192.0.2.0/24',)
    assert client_ip(request,env['config'])=='8.8.8.8'


def test_secure_cookie_production_default(env):
    from fastapi.testclient import TestClient
    from pulsefeed.main import create_app
    env['config'].cookie_secure=True
    app=create_app(env['config'],env['provider'])
    with TestClient(app,base_url='https://testserver') as client:
        url=issue_link(env['db'],env['config'],env['other']).replace(env['config'].base_url,'')
        response=client.get(url,follow_redirects=False)
        assert '; Secure' in response.headers['set-cookie']
        assert client.get('/api/session').status_code==200
