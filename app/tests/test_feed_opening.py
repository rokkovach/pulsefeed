"""The navigation-first shell must never admit or expose a saved feed on GET."""
import pytest

from pulsefeed.auth import issue_link
from test_view_feed import source


def snapshot(env):
    return {table: env['db'].all('SELECT * FROM ' + table)
            for table in ('feeds', 'items', 'feed_views', 'batches', 'runs', 'invocations', 'rate_events')}


def test_opening_shell_is_read_only_and_does_not_restore_saved_feeds(env):
    env['app'].state.service.view_feed(env['owner'], 'demo', 'old-opening-view', source(('old-story-sentinel',)))
    before = snapshot(env)
    for _ in range(2):
        response = env['client'].get('/feeds/new')
        assert response.status_code == 200
        assert response.context['pending_feed'] is True
        assert response.context['view'] is None and response.context['initial_feed'] is None
        assert 'old-story-sentinel' not in response.text
        assert response.headers['cache-control'] == 'no-store'
        assert response.headers['referrer-policy'] == 'no-referrer'
    assert snapshot(env) == before
    assert env['provider'].submissions == []


@pytest.mark.parametrize('public', [False, True])
def test_opening_shell_requires_existing_session_even_in_public_mode(env, public):
    env['config'].public_access_enabled = public
    env['client'].cookies.clear()
    accounts = env['db'].all('SELECT * FROM accounts')
    assert env['client'].get('/feeds/new').status_code == 401
    assert env['db'].all('SELECT * FROM accounts') == accounts
    assert env['provider'].submissions == []


def test_opening_shell_preserves_admin_role_gate(env):
    assert env['client'].get('/feeds/new?admin').status_code == 403
    env['client'].get(issue_link(env['db'], env['config'], env['admin']).replace(env['config'].base_url, ''))
    response = env['client'].get('/feeds/new?admin')
    assert response.status_code == 200 and response.context['admin_mode'] is True
    assert response.context['pending_feed'] is True
    assert env['provider'].submissions == []


def test_opening_shell_does_not_take_request_data_from_url(env):
    before = snapshot(env)
    response = env['client'].get('/feeds/new?url=https://example.com/private-feed&key=untrusted-input')
    assert response.status_code == 200
    assert 'private-feed' not in response.text and 'untrusted-input' not in response.text
    assert snapshot(env) == before
    assert env['provider'].submissions == []
