"""Article layout is versionless user-message data; system instructions stay separate."""
import json
import time

import pytest

from pulsefeed.auth import issue_link
from pulsefeed.db import Database, DEFAULT_QWEN_ARTICLE_TEMPLATE, DEFAULT_SETTINGS, dump
from pulsefeed.errors import AppError
from pulsefeed.service import validate_settings
from test_jobs import finish
from test_view_feed import source

CUSTOM = 'Excerpt: {{article_excerpt}}\nTitle: {{article_title}}\nUnknown: {{literal}}'
FIELDS = ('qwen_template', 'qwen_article_template', 'flux_prefix', 'fallback_prompt', 'blocked_terms')


def admin_headers(env):
    env['client'].get(issue_link(env['db'], env['config'], env['admin']).replace(env['config'].base_url, ''))
    return {'X-CSRF-Token': env['client'].get('/api/session').json()['csrf']}


def admin_body(**overrides):
    return dict({key: DEFAULT_SETTINGS[key] for key in FIELDS}, **overrides)


def test_default_and_marker_budget_are_exact_unicode_codepoints():
    assert DEFAULT_QWEN_ARTICLE_TEMPLATE == 'Article data:\n{"title": {{article_title}}, "excerpt": {{article_excerpt}}}'
    assert len('{{article_title}}') + len('{{article_excerpt}}') == 36
    template = '🌱'*2000 + '{{article_excerpt}}{{article_title}}'
    settings = validate_settings(dict(DEFAULT_SETTINGS, qwen_article_template=template))
    assert len(settings['qwen_article_template']) == 2036
    with pytest.raises(AppError):
        validate_settings(dict(DEFAULT_SETTINGS, qwen_article_template='🌱'+template))


@pytest.mark.parametrize('template', [
    '', '{{article_title}}', '{{article_excerpt}}',
    '{{article_title}} {{article_title}} {{article_excerpt}}',
    '{{article_title}} {{article_excerpt}} {{article_excerpt}}',
    '{{ article_title }} {{article_excerpt}}', None, 5, [],
    '{{article_title}} {{article_excerpt}}\x00',
    '{{article_title}} {{article_excerpt}}\ud800',
])
def test_invalid_article_layout_is_rejected_as_settings_error(template):
    with pytest.raises(AppError) as error:
        validate_settings(dict(DEFAULT_SETTINGS, qwen_article_template=template))
    assert error.value.code == 'INVALID_SETTINGS' and error.value.status == 400


def test_validate_keeps_unknown_markers_literal_and_never_changes_system():
    settings = dict(DEFAULT_SETTINGS, qwen_template='System literal {{article_title}} remains trusted text.', qwen_article_template=CUSTOM)
    assert validate_settings(settings) == settings
    assert settings['qwen_article_template'] == CUSTOM
    legacy = dict(settings)
    del legacy['qwen_article_template']
    assert 'qwen_article_template' not in validate_settings(legacy)
    body = admin_body()
    del body['qwen_article_template']
    assert validate_settings(body, from_admin=True)['qwen_article_template'] == DEFAULT_QWEN_ARTICLE_TEMPLATE
    assert validate_settings(body, from_admin=True, current_settings=settings)['qwen_article_template'] == CUSTOM


def test_current_migration_is_once_and_never_rewrites_legacy_snapshots(env):
    current = dict(DEFAULT_SETTINGS)
    del current['qwen_article_template']
    current['qwen_template'] = 'Historical system literal {{article_title}}'
    env['db'].execute('UPDATE settings SET value=?', (dump(current),))
    saved = env['app'].state.service.view_feed(env['owner'], 'demo', 'qwen-legacy-snapshot', source(('one',)))
    run_id = saved['runs'][0]['id']
    original_settings = env['db'].one('SELECT settings FROM runs WHERE id=?', (run_id,))['settings']
    assert 'qwen_article_template' not in json.loads(original_settings)
    run = env['db'].one('SELECT * FROM runs WHERE id=?', (run_id,))
    run['deadline'] = time.time()+600
    invocation = env['app'].state.coordinator.prepare(run, 'qwen')
    original_input = invocation['input']
    assert 'article_template' not in json.loads(original_input)
    Database(env['config'].database_path)
    migrated = env['db'].settings()
    assert migrated == dict(current, qwen_article_template=DEFAULT_QWEN_ARTICLE_TEMPLATE)
    assert env['db'].one('SELECT settings FROM runs WHERE id=?', (run_id,))['settings'] == original_settings
    assert env['db'].one('SELECT input FROM invocations WHERE id=?', (invocation['id'],))['input'] == original_input
    updated = env['db'].one('SELECT updated FROM settings WHERE id=1')['updated']
    Database(env['config'].database_path)
    assert env['db'].one('SELECT updated FROM settings WHERE id=1')['updated'] == updated


@pytest.mark.parametrize('layout', [None, DEFAULT_QWEN_ARTICLE_TEMPLATE, CUSTOM])
def test_coordinator_uses_saved_layout_only_and_never_interpolates_system(env, layout):
    settings = dict(DEFAULT_SETTINGS, qwen_template='System instructions independent from article data.')
    if layout is None:
        del settings['qwen_article_template']
    else:
        settings['qwen_article_template'] = layout
    env['db'].execute('UPDATE settings SET value=?', (dump(settings),))
    fetched = source(('one',))
    fetched['items'][0]['article']['title'] = 'Quoted "article" with {{article_excerpt}} marker'
    saved = env['app'].state.service.view_feed(env['owner'], 'demo', 'qwen-payload-shape', fetched)
    env['db'].execute('UPDATE settings SET value=?', (dump(dict(DEFAULT_SETTINGS, qwen_article_template='Changed {{article_title}} {{article_excerpt}}')),))
    run = env['db'].one('SELECT * FROM runs WHERE id=?', (saved['runs'][0]['id'],))
    run['deadline'] = time.time()+600
    invocation = env['app'].state.coordinator.prepare(run, 'qwen')
    payload = json.loads(invocation['input'])
    expected = {'schema_version','stage','run_id','invocation_id','deadline_unix_ms','article','system_prompt','max_new_tokens'}
    assert set(payload) == (expected | {'article_template'} if layout == CUSTOM else expected)
    assert payload['system_prompt'] == settings['qwen_template']
    assert payload['article'] == fetched['items'][0]['article']
    assert 'Quoted' not in payload['system_prompt']
    assert ('article_template' in payload) == (layout == CUSTOM)
    if layout == CUSTOM:
        assert payload['article_template'] == CUSTOM
    assert env['provider'].submissions == []


async def test_qwen_retry_keeps_original_article_layout_and_settings(env):
    original = dict(DEFAULT_SETTINGS, qwen_article_template=CUSTOM)
    env['db'].execute('UPDATE settings SET value=?', (dump(original),))
    service = env['app'].state.service
    saved = service.view_feed(env['owner'], 'demo', 'qwen-retry-layout', source(('one',)))
    env['provider'].behavior['qwen'] = 'failure'
    await finish(env)
    old = saved['runs'][0]['id']
    env['db'].execute('UPDATE settings SET value=?', (dump(DEFAULT_SETTINGS),))
    retry = service.retry_view(env['owner'], saved['view']['id'], old, 'qwen-retry-new-key')
    retry_run = retry['runs'][0]
    assert json.loads(env['db'].one('SELECT settings FROM runs WHERE id=?', (retry_run['id'],))['settings'])['qwen_article_template'] == CUSTOM
    env['provider'].behavior.clear()
    await finish(env)
    qwen_inputs = [payload for stage, payload in env['provider'].submissions if stage == 'qwen']
    assert len(qwen_inputs) == 2
    assert all(payload['article_template'] == CUSTOM for payload in qwen_inputs)
    assert all(payload['system_prompt'] == original['qwen_template'] for payload in qwen_inputs)


def test_admin_api_new_field_and_legacy_call_preservation_and_auth(env):
    body = admin_body(qwen_article_template=CUSTOM)
    assert env['client'].post('/api/settings', json=body, headers=env['headers']).status_code == 403
    headers = admin_headers(env)
    assert env['client'].post('/api/settings', json=body).status_code == 403
    response = env['client'].post('/api/settings', json=body, headers=headers)
    assert response.status_code == 200 and response.json()['qwen_article_template'] == CUSTOM
    old_body = admin_body(qwen_template='Updated system only.')
    del old_body['qwen_article_template']
    response = env['client'].post('/api/settings', json=old_body, headers=headers)
    assert response.status_code == 200
    assert response.json()['qwen_article_template'] == CUSTOM
    assert response.json()['qwen_template'] == 'Updated system only.'
    invalid = dict(body, qwen_article_template='Missing article tokens')
    before = env['db'].settings()
    response = env['client'].post('/api/settings', json=invalid, headers=headers)
    assert response.status_code == 400 and response.json()['error']['code'] == 'INVALID_SETTINGS'
    assert env['db'].settings() == before
