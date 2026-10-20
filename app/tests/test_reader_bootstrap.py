import json
import pytest

from pulsefeed.auth import issue_link
from pulsefeed.rss import source_hostname
from test_jobs import finish
from test_view_feed import source


@pytest.mark.parametrize(('link','expected'),[
    ('https://WWW.Example.COM:443/path','example.com'),
    ('https://name:password@www.Example.com:8080/path','example.com'),
    ('https://www.bücher.example/path','xn--bcher-kva.example'),
    ('http://faß.de','xn--fa-hia.de'),
    ('http://[2001:0db8::1]:8080/path','[2001:db8::1]'),
    ('http://[::1]/','[::1]'),
    ('javascript:alert(1)',''),('file:///tmp/source',''),
    ('http://[',''),('http://example.com:invalid',''),(None,''),
])
def test_source_hostname_safe_display_normalization(link,expected):
    assert source_hostname(link) == expected


def test_initial_empty_account_never_borrows_another_feed(env):
    service = env['app'].state.service
    foreign = service.view_feed(env['other'],'https://example.com/private','private-view-key',source(('private-sentinel',)))
    response = env['client'].get(foreign['view']['url'])
    assert response.status_code == 404
    assert 'initial_feed' not in response.context
    assert 'private-sentinel' not in response.text


def test_view_url_selection_and_legacy_identifiers_do_not_restore(env):
    service = env['app'].state.service
    first = service.view_feed(env['owner'],'https://example.com/first','first-view-key',source(('first',)))
    foreign = service.view_feed(env['other'],'https://example.com/foreign','foreign-view-key',source(('private-sentinel',)))
    selected = env['client'].get(first['view']['url'])
    assert selected.context['initial_feed']['items'] == first['items']
    assert selected.context['view'] == first['view']
    home = env['client'].get('/')
    assert home.context['initial_feed'] is None
    assert 'feeds' not in home.context
    for path in ('/?feed=invalid','/?feed='+first['feed']['id'],foreign['view']['url'],'/feeds/invalid'):
        response = env['client'].get(path)
        assert response.status_code == 404
        assert 'private-sentinel' not in response.text


async def test_initial_snapshot_latest_run_metadata_and_gets_do_not_submit(env):
    service = env['app'].state.service
    saved = service.view_feed(env['owner'],'demo','bootstrap-view-key',source())
    await finish(env)
    before = {table:env['db'].all('SELECT * FROM '+table) for table in ('runs','batches','invocations','feed_views','rate_events','quota_policy')}
    submitted = len(env['provider'].submissions)
    for _ in range(3):
        response = env['client'].get(saved['view']['url'])
        initial = response.context['initial_feed']
        assert initial['items'] == saved['items'] and initial['view'] == saved['view']
        assert len(initial['runs']) == 5 and all(run['state']=='completed' for run in initial['runs'])
        assert initial['runs'][0]['invocations'][-1]['stage'] == 'flux'
        assert 'image_base64' not in json.dumps(initial)
    assert len(env['provider'].submissions) == submitted
    assert before == {table:env['db'].all('SELECT * FROM '+table) for table in before}


async def test_initial_snapshot_reads_feed_runs_and_active_batch_consistently(env,monkeypatch):
    service = env['app'].state.service
    saved = service.view_feed(env['owner'],'demo','consistent-view-key',source(('first',)))
    await finish(env)
    original = service._view_result
    admitted = []
    def after_feed_read(conn,owner,row,reused=False):
        if not admitted:
            admitted.append(service.retry_view(owner,row['id'],saved['runs'][0]['id'],'bootstrap-concurrent-key'))
        return original(conn,owner,row,reused)
    monkeypatch.setattr(service,'_view_result',after_feed_read)
    response = env['client'].get(saved['view']['url'])
    initial = response.context['initial_feed']
    assert initial['items'] == saved['items']
    assert initial['runs'][0]['id'] == saved['runs'][0]['id'] and initial['active_batch_id'] is None
    next_snapshot = env['client'].get(saved['view']['url']).context['initial_feed']
    assert len(next_snapshot['runs']) == 1
    assert next_snapshot['active_batch_id'] == admitted[0]['id']


def test_initial_snapshot_cap_membership_and_order(env):
    service = env['app'].state.service
    saved = service.view_feed(env['owner'],'demo','cap-membership-key',source(tuple(str(i) for i in range(13))))
    assert len(env['client'].get(saved['view']['url']).context['initial_feed']['items']) == 10
    service.save_feed(env['owner'],'demo',source(('2','1')))
    initial = env['client'].get(saved['view']['url']).context['initial_feed']
    assert [item['title'] for item in initial['items']] == [str(i) for i in range(10)]


def test_initial_snapshot_retains_admin_role_gates(env):
    service = env['app'].state.service
    service.save_feed(env['owner'],'demo',source(('evaluator-sentinel',)))
    assert env['client'].get('/?admin&feed=anything').status_code == 403
    env['client'].get(issue_link(env['db'],env['config'],env['admin']).replace(env['config'].base_url,''))
    saved = service.view_feed(env['admin'],'demo','admin-view-key',source(('admin-story',)))
    response = env['client'].get(saved['view']['url']+'?admin')
    assert response.context['admin_mode'] and response.context['settings']
    assert response.context['initial_feed']['view'] == saved['view']
    assert 'evaluator-sentinel' not in response.text
    env['client'].cookies.clear()
    assert env['client'].get(saved['view']['url']).status_code == 401


@pytest.mark.parametrize('role',['evaluator','admin'])
def test_bare_home_with_saved_feeds_is_blank_read_only_and_preserves_explicit_restore(env,role):
    owner = env['admin'] if role=='admin' else env['owner']
    if role=='admin':
        env['client'].get(issue_link(env['db'],env['config'],owner).replace(env['config'].base_url,''))
    service = env['app'].state.service
    saved = service.view_feed(owner,'https://example.com/saved','home-saved-view',source(('saved-story',)))
    tables = [row['name'] for row in env['db'].all("SELECT name FROM sqlite_master WHERE type='table'")]
    def snapshot():
        return {table:env['db'].all('SELECT * FROM '+table) for table in tables}
    before = snapshot()
    for path in (['/','/?admin'] if role=='admin' else ['/']):
        response = env['client'].get(path)
        assert response.status_code == 200
        assert response.context['initial_feed'] is None
        assert 'feeds' not in response.context and response.context['view'] is None
        assert response.context['admin_mode'] == (path=='/?admin')
        assert 'data-item-id=' not in response.text
    selected = env['client'].get(saved['view']['url'])
    assert selected.context['initial_feed']['items'] == saved['items']
    assert 'data-item-id=' in selected.text
    assert before == snapshot()
    assert env['provider'].submissions == []
