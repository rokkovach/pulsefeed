"""Password sign-in and atomic section saves exercise temp DBs and no model work."""
from concurrent.futures import ThreadPoolExecutor
import threading
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from pulsefeed.auth import issue_link, login_challenge, verify_login_challenge
from pulsefeed.db import DEFAULT_SETTINGS, dump
from test_public_admin_auth import public, ADMIN_TOKEN, ROTATED_TOKEN, bootstrap
from test_view_feed import source


COOKIE = 'pulsefeed_admin_login'


def form(client):
    response = client.get('/admin')
    assert response.status_code == 200 and response.template.name == 'admin_login.html'
    return response.context['login_csrf']


def sign_in(public, password=None, client=None, nonce=None, headers=None):
    client = client or public['client']
    nonce = nonce or form(client)
    return client.post('/admin/login',data={'password':public['config'].admin_access_token if password is None else password,'login_csrf':nonce},
                       headers=headers or {}, follow_redirects=False)


def admin_headers(env):
    env['client'].get(issue_link(env['db'],env['config'],env['admin']).replace(env['config'].base_url,''))
    return {'X-CSRF-Token':env['client'].get('/api/session').json()['csrf']}


def test_anonymous_admin_bookmark_is_one_password_form_without_minting_or_secret(public):
    response = public['client'].get('/admin')
    assert response.status_code == 200 and response.template.name == 'admin_login.html'
    assert response.text.count('type="password"') == 1
    assert 'name="login_csrf"' in response.text and 'name="password"' in response.text
    assert 'value="'+ADMIN_TOKEN+'"' not in response.text
    for secret in (ADMIN_TOKEN,public['config'].session_secret):
        assert secret not in response.text+str(response.headers)
    cookie = response.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'SameSite=lax' in cookie and 'Path=/admin' in cookie
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['referrer-policy'] == 'same-origin'
    assert '<meta name="referrer" content="same-origin">' in response.text
    assert public['db'].all('SELECT * FROM accounts') == public['db'].all('SELECT * FROM sessions') == []
    assert public['provider'].submissions == []


def test_login_preserves_valid_visitor_owner_and_views_but_rotates_session_and_csrf(public):
    client = public['client']; session = bootstrap(client)
    saved = public['app'].state.service.view_feed(session['account']['id'],'demo','login-owner-view',source(('one',)))
    old_cookie = client.cookies.get('pulsefeed_session')
    response = sign_in(public,headers={'Origin':public['config'].base_url})
    assert response.status_code == 303 and response.headers['location'] == '/admin'
    assert response.headers['referrer-policy'] == 'no-referrer'
    assert client.cookies.get(COOKIE) is None
    assert client.cookies.get('pulsefeed_session') != old_cookie
    elevated = client.get('/api/session').json()
    assert elevated['account']['id'] == session['account']['id'] and elevated['account']['role'] == 'admin'
    assert elevated['csrf'] != session['csrf']
    assert client.get(saved['view']['url']).status_code == 200
    assert public['db'].one('SELECT role FROM accounts WHERE id=?',(session['account']['id'],))['role'] == 'evaluator'
    dashboard = client.get('/admin')
    assert dashboard.context['settings'] == public['db'].settings()
    assert dashboard.headers['referrer-policy'] == 'no-referrer'
    assert '<meta name="referrer" content="no-referrer">' in dashboard.text
    with TestClient(public['app']) as stale:
        stale.cookies.set('pulsefeed_session',old_cookie)
        assert stale.get('/api/session').status_code == 401
    assert ADMIN_TOKEN not in response.text+str(response.headers)+dashboard.text
    assert public['provider'].submissions == []


def test_separate_fresh_login_browsers_get_distinct_owners(public):
    assert sign_in(public).status_code == 303
    first = public['client'].get('/api/session').json()
    with TestClient(public['app']) as other:
        assert sign_in(public,client=other).status_code == 303
        second = other.get('/api/session').json()
    assert first['account']['id'] != second['account']['id']
    assert all(row['role']=='evaluator' for row in public['db'].all('SELECT role FROM accounts'))


def test_wrong_password_keeps_existing_visitor_session_and_fixed_error_never_echoes_input(public):
    before = bootstrap(public['client'])
    old_cookie = public['client'].cookies.get('pulsefeed_session')
    response = sign_in(public,password='wrong-password-sentinel')
    assert response.status_code == 401 and response.template.name == 'admin_login.html'
    assert response.headers['referrer-policy'] == 'same-origin'
    assert '<meta name="referrer" content="same-origin">' in response.text
    assert 'password didn’t work' in response.text
    assert 'wrong-password-sentinel' not in response.text
    assert public['client'].cookies.get('pulsefeed_session') == old_cookie
    assert public['client'].get('/api/session').json() == before
    assert len(public['db'].all("SELECT * FROM rate_events WHERE action='admin_exchange'")) == 1
    assert public['provider'].submissions == []


@pytest.mark.parametrize('headers', [
    {'Origin':'https://evil.example'}, {'Origin':'null'},
    {'Origin':'http://127.0.0.1:8000/path'}, {'Referer':'https://evil.example/admin'},
    {'Sec-Fetch-Site':'cross-site'},
    [('Origin','http://127.0.0.1:8000'),('Origin','https://evil.example')],
])
def test_login_rejects_cross_origin_or_invalid_origin_even_with_valid_challenge(public,headers):
    response = sign_in(public,headers=headers)
    assert response.status_code == 403
    assert public['db'].all('SELECT * FROM accounts') == public['db'].all('SELECT * FROM sessions') == []
    assert len(public['db'].all("SELECT * FROM rate_events WHERE action='admin_exchange'")) == 1


@pytest.mark.parametrize('alteration', ['missing','forged-cookie','mismatch','expired','duplicate','extra','invalid-utf8','too-large','wrong-type'])
def test_login_requires_bounded_exact_form_and_signed_matching_unexpired_challenge(public,alteration):
    client = public['client']; nonce = form(client)
    pairs = [('password',ADMIN_TOKEN),('login_csrf',nonce)]
    headers = {'Content-Type':'application/x-www-form-urlencoded'}
    expected = 403
    if alteration == 'missing':client.cookies.clear()
    elif alteration == 'forged-cookie':client.cookies.set(COOKIE,'f'*43+'.9999999999.'+'0'*64,path='/admin')
    elif alteration == 'mismatch':pairs[1] = ('login_csrf','x'*43)
    elif alteration == 'expired':
        nonce,cookie = login_challenge(public['config'],now=0)
        client.cookies.set(COOKIE,cookie,domain='testserver.local',path='/admin')
        pairs[1] = ('login_csrf',nonce)
    elif alteration == 'duplicate':pairs.append(('password',ADMIN_TOKEN))
    elif alteration == 'extra':pairs.append(('extra','field'))
    elif alteration == 'wrong-type':headers['Content-Type'] = 'application/json'
    content = b'password=\xff&login_csrf=bad' if alteration == 'invalid-utf8' else b'x'*2049 if alteration == 'too-large' else urlencode(pairs).encode()
    if alteration == 'too-large':expected = 413
    response = client.post('/admin/login',content=content,headers=headers,follow_redirects=False)
    assert response.status_code == expected
    assert public['db'].all('SELECT * FROM accounts') == public['db'].all('SELECT * FROM sessions') == []
    assert ADMIN_TOKEN not in response.text+str(response.headers)


def test_bad_login_attempts_commit_throttle_and_share_limit_with_query_grants(public):
    public['config'].exchange_limit = 2
    assert sign_in(public,password='bad').status_code == 401
    assert sign_in(public,password='bad').status_code == 401
    denied = sign_in(public)
    assert denied.status_code == 429 and int(denied.headers['retry-after']) > 0
    assert public['db'].all('SELECT * FROM accounts') == []
    response = public['client'].get('/',params={public['config'].admin_access_param:ADMIN_TOKEN},follow_redirects=False)
    assert response.status_code == 303 and response.headers.get('retry-after')


def test_origin_port_zero_does_not_alias_the_default_port(public):
    public['config'].base_url = 'http://127.0.0.1'
    assert sign_in(public,headers={'Origin':'http://127.0.0.1:0'}).status_code == 403


def test_referrer_policy_override_is_only_for_rendered_login_forms(public):
    for path in ('/','/access','/feeds/missing'):
        response = public['client'].get(path)
        assert response.headers['referrer-policy'] == 'no-referrer'
        assert '<meta name="referrer" content="no-referrer">' in response.text
        assert '<meta name="referrer" content="same-origin">' not in response.text
    for password in ('incorrect',ADMIN_TOKEN):
        response = public['client'].get('/',params={public['config'].admin_access_param:password},follow_redirects=False)
        assert response.status_code == 303 and response.headers['location'] == '/admin'
        assert response.headers['referrer-policy'] == 'no-referrer'


def test_admin_logout_returns_to_sign_in_and_challenge_cookie_is_secure_when_configured(public):
    assert sign_in(public).status_code == 303
    csrf = public['client'].get('/api/session').json()['csrf']
    response = public['client'].post('/logout',headers={'X-CSRF-Token':csrf},follow_redirects=False)
    assert response.status_code == 303 and response.headers['location'] == '/admin'
    public['config'].cookie_secure = True
    response = public['client'].get('/admin')
    assert 'Secure' in response.headers['set-cookie'] and 'HttpOnly' in response.headers['set-cookie']


def test_challenge_binding_expiry_and_rotation_are_checked_without_secret_echo(public):
    nonce,cookie = login_challenge(public['config'],now=1000)
    assert verify_login_challenge(public['config'],cookie,nonce,now=1000)
    assert not verify_login_challenge(public['config'],cookie,nonce,now=1600)
    assert not verify_login_challenge(public['config'],cookie,nonce,now=999)
    public['config'].admin_access_token = ROTATED_TOKEN
    assert not verify_login_challenge(public['config'],cookie,nonce,now=1000)
    assert sign_in(public,password=ADMIN_TOKEN).status_code == 401
    assert sign_in(public).status_code == 303
    public['config'].admin_access_token = ''
    assert public['client'].get('/api/session').status_code == 401
    disabled = public['client'].get('/admin')
    assert disabled.context['login_enabled'] is False and 'Admin sign-in is not configured.' in disabled.text


def test_section_settings_strict_fields_auth_csrf_and_preservation(env):
    client = env['client']; before = env['db'].settings()
    qwen = {'qwen_template':'System instructions.','qwen_article_template':'Title: {{article_title}}\nExcerpt: {{article_excerpt}}'}
    assert client.post('/api/admin/settings/qwen',json=qwen,headers=env['headers']).status_code == 403
    headers = admin_headers(env)
    assert client.post('/api/admin/settings/qwen',json=qwen).status_code == 403
    result = client.post('/api/admin/settings/qwen',json=qwen,headers=headers)
    assert result.status_code == 200 and result.json() == dict(before,**qwen)
    for section,body in [('qwen',{'qwen_template':'Only one field'}),('flux',{'flux_prefix':'{{qwen_prompt}}','blocked_terms':'extra'}),('content',{'blocked_terms':''}),('unknown',{})]:
        assert client.post('/api/admin/settings/'+section,json=body,headers=headers).status_code == 400
    assert env['db'].settings() == dict(before,**qwen)
    content = {'fallback_prompt':'A quiet garden.','blocked_terms':'prohibited'}
    result = client.post('/api/admin/settings/content',json=content,headers=headers)
    assert result.status_code == 200 and result.json() == dict(before,**qwen,**content)
    invalid = dict(content,blocked_terms='quiet')
    assert client.post('/api/admin/settings/content',json=invalid,headers=headers).status_code == 400
    assert env['db'].settings() == dict(before,**qwen,**content)
    assert env['provider'].submissions == []


def test_qwen_content_sections_preserve_legacy_flux_until_explicit_flux_save(env):
    headers = admin_headers(env)
    legacy = dict(DEFAULT_SETTINGS,flux_prefix='Historical literal {{qwen_prompt}}',flux_template_version=0)
    env['db'].execute('UPDATE settings SET value=?',(dump(legacy),))
    qwen = {'qwen_template':'New system text.','qwen_article_template':DEFAULT_SETTINGS['qwen_article_template']}
    for section,body in [('qwen',qwen),('content',{'fallback_prompt':'A quiet landscape.','blocked_terms':''})]:
        response = env['client'].post('/api/admin/settings/'+section,json=body,headers=headers)
        assert response.status_code == 200
        assert response.json()['flux_template_version'] == 0 and response.json()['flux_prefix'] == legacy['flux_prefix']
    response = env['client'].post('/api/admin/settings/flux',json={'flux_prefix':'Before {{qwen_prompt}} after'},headers=headers)
    assert response.status_code == 200 and response.json()['flux_template_version'] == 1


def test_atomic_section_writes_preserve_other_concurrent_updates(env):
    service = env['app'].state.service; barrier = threading.Barrier(2)
    qwen = {'qwen_template':'Concurrent system.','qwen_article_template':'{{article_excerpt}} then {{article_title}}'}
    flux = {'flux_prefix':'Concurrent style {{qwen_prompt}} ending.'}
    original = env['db'].settings()
    def save(pair):
        barrier.wait()
        return service.save_settings(pair[1],pair[0])
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(save,[('qwen',qwen),('flux',flux)]))
    assert env['db'].settings() == dict(original,**qwen,**flux)


def test_legacy_and_section_settings_share_committed_write_limit(env):
    headers = admin_headers(env);env['config'].admin_write_limit = 1
    response = env['client'].post('/api/admin/settings/flux',json={'flux_prefix':'Style {{qwen_prompt}}'},headers=headers)
    assert response.status_code == 200
    full = {key:value for key,value in DEFAULT_SETTINGS.items() if key!='flux_template_version'}
    response = env['client'].post('/api/settings',json=full,headers=headers)
    assert response.status_code == 429 and response.headers.get('retry-after')
    assert env['db'].settings()['flux_prefix'] == 'Style {{qwen_prompt}}'
