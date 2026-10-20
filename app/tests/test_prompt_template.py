import json
import pytest
from pulsefeed.db import Database,DEFAULT_SETTINGS,dump
from pulsefeed.errors import AppError
from pulsefeed.service import compose_prompt,validate_settings
from conftest import start
from test_jobs import finish


def test_before_after_and_literal_unknown_placeholder():
    settings=dict(DEFAULT_SETTINGS,flux_prefix='Before: {{qwen_prompt}}. After {{literal}}.')
    final,matched,fallback=compose_prompt('Visual scene',settings)
    assert final=='Before: Visual scene. After {{literal}}.'
    assert matched==[] and not fallback
    settings['flux_prefix']='{{qwen_prompt}} then suffix'
    assert compose_prompt('Scene',settings)[0]=='Scene then suffix'


@pytest.mark.parametrize('template',['Just a prefix','{{qwen_prompt}} {{qwen_prompt}}','{{ qwen_prompt }}',''])
def test_new_settings_require_one_literal_token(template):
    with pytest.raises(AppError) as error:validate_settings(dict(DEFAULT_SETTINGS,flux_prefix=template))
    assert error.value.code=='INVALID_SETTINGS'


def test_static_budget_excludes_token_and_composition_is_bounded():
    settings=dict(DEFAULT_SETTINGS,flux_prefix='x'*2000+'{{qwen_prompt}}')
    validate_settings(settings)
    assert len(compose_prompt('y'*2000,settings)[0])==4000
    with pytest.raises(AppError):validate_settings(dict(settings,flux_prefix='x'*2001+'{{qwen_prompt}}'))
    with pytest.raises(AppError):compose_prompt('y'*2001,settings)


@pytest.mark.parametrize('template',['blocked {{qwen_prompt}} end','start {{qwen_prompt}} blocked'])
def test_filter_checks_prefix_and_suffix(template):
    settings=dict(DEFAULT_SETTINGS,flux_prefix=template,blocked_terms='BLOCKED')
    final,matched,fallback=compose_prompt('a scene',settings)
    assert fallback and matched==['blocked'] and final==settings['fallback_prompt']
    assert '{{qwen_prompt}}' not in final


def test_legacy_current_migration_is_once_and_snapshots_are_untouched(env):
    legacy=dict(DEFAULT_SETTINGS,flux_prefix='Legacy prefix');legacy.pop('flux_template_version')
    env['db'].execute('UPDATE settings SET value=?',(dump(legacy),))
    Database(env['config'].database_path)
    assert env['db'].settings()['flux_prefix']=='Legacy prefix {{qwen_prompt}}'
    Database(env['config'].database_path)
    assert env['db'].settings()['flux_prefix'].count('{{qwen_prompt}}')==1
    assert compose_prompt('a scene',legacy)[0]=='Legacy prefix a scene'
    with pytest.raises(AppError):validate_settings(legacy,from_admin=True)


async def test_active_legacy_snapshot_and_image_retry_stay_original(env):
    batch=start(env)
    run_id=batch['runs'][0]['id']
    legacy=dict(DEFAULT_SETTINGS,flux_prefix='Legacy editorial prefix');legacy.pop('flux_template_version')
    env['db'].execute('UPDATE runs SET settings=? WHERE id=?',(dump(legacy),run_id))
    env['provider'].behavior['flux']='failure'
    await finish(env)
    old=env['app'].state.service.run(env['owner'],run_id)
    assert old['final_prompt']=='Legacy editorial prefix '+old['qwen_prompt']
    env['provider'].behavior.clear()
    response=env['client'].post('/api/runs/'+run_id+'/retry?view_id='+env['view_id'],json={'idempotency_key':'legacy-image-retry'},headers=env['headers'])
    assert response.status_code==200
    await finish(env)
    new=env['app'].state.service.batch(env['owner'],response.json()['id'])['runs'][0]
    assert new['state']=='completed' and new['final_prompt']==old['final_prompt']
    assert [stage for stage,_ in env['provider'].submissions]==['qwen','flux','flux']


def test_full_legacy_prefix_migration_fits_new_budget(env):
    legacy=dict(DEFAULT_SETTINGS,flux_prefix='x'*1900);legacy.pop('flux_template_version')
    env['db'].execute('UPDATE settings SET value=?',(dump(legacy),))
    Database(env['config'].database_path)
    settings=env['db'].settings()
    assert settings['flux_template_version']==1
    validate_settings(settings)
    final,_,_=compose_prompt('y'*2000,settings)
    assert final=='x'*1900+' '+'y'*2000 and len(final)==3901


def test_unversioned_historical_reserved_literal_is_not_a_token():
    legacy=dict(DEFAULT_SETTINGS,flux_prefix='Before {{qwen_prompt}} after');legacy.pop('flux_template_version')
    final,_,_=compose_prompt('actual scene',legacy)
    assert final=='Before {{qwen_prompt}} after actual scene'
    versioned=dict(legacy,flux_template_version=1)
    assert compose_prompt('actual scene',versioned)[0]=='Before actual scene after'


async def test_current_reserved_literal_stays_legacy_until_admin_save(env):
    from pulsefeed.auth import issue_link
    legacy=dict(DEFAULT_SETTINGS,flux_prefix='Before {{qwen_prompt}} after');legacy.pop('flux_template_version')
    env['db'].execute('UPDATE settings SET value=?',(dump(legacy),))
    Database(env['config'].database_path)
    current=env['db'].settings()
    assert current['flux_template_version']==0 and current['flux_prefix']==legacy['flux_prefix']
    batch=start(env);await finish(env)
    run=env['app'].state.service.batch(env['owner'],batch['id'])['runs'][0]
    assert run['final_prompt']=='Before {{qwen_prompt}} after '+run['qwen_prompt']
    env['client'].get(issue_link(env['db'],env['config'],env['admin']).replace(env['config'].base_url,''))
    headers={'X-CSRF-Token':env['client'].get('/api/session').json()['csrf']}
    response=env['client'].post('/api/settings',json=legacy,headers=headers)
    assert response.status_code==200 and response.json()['flux_template_version']==1
    assert env['db'].settings()['flux_template_version']==1
    downgrade=env['client'].post('/api/settings',json=dict(legacy,flux_template_version=0),headers=headers)
    assert downgrade.status_code==400
