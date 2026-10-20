import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from pulsefeed.coordinator import Coordinator
from pulsefeed.errors import AppError
from pulsefeed.service import compose_prompt
from pulsefeed.db import DEFAULT_SETTINGS
from conftest import start,add_demo


async def finish(env,limit=64):
    for _ in range(limit):
        await env['app'].state.coordinator.tick()
        if not env['db'].one("SELECT 1 FROM batches WHERE state='active'"):
            break


async def test_pipeline_completion_metadata_no_bytes_and_refresh(env):
    batch=start(env,count=3)
    await finish(env)
    result=env['app'].state.service.batch(env['owner'],batch['id'])
    assert result['state']=='finished'
    assert all(run['state']=='completed' for run in result['runs'])
    assert len(env['provider'].submissions)==6
    assert env['db'].all('SELECT reserved FROM runs')==[{'reserved':1}]*3
    rows=env['db'].all('SELECT output FROM invocations')
    assert all('image_base64' not in row['output'] for row in rows)
    run=result['runs'][0]
    response=env['client'].get('/api/runs/'+run['id']+'/image?view_id='+env['view_id'])
    assert response.status_code==200 and response.headers['content-type']=='image/webp'
    env['client'].get(env['view_data']['view']['url']);env['client'].get('/api/batches/'+batch['id']+'?view_id='+env['view_id'])
    assert len(env['provider'].submissions)==6
    assert all(inv['model']['revision']=='synthetic-fixture' for inv in run['invocations'])


async def test_duplicate_busy_and_quota(env):
    batch=start(env)
    service=env['app'].state.service
    same=service.cached_view(env['owner'],'sample-key-0001','demo')['generation']['batch']
    assert same['id']==batch['id']
    with pytest.raises(AppError) as error:
        service.admit(env['owner'],[batch['runs'][0]['item_id']],'new-key-002')
    assert error.value.status==429
    await finish(env)
    env['config'].image_quota=2
    with pytest.raises(AppError) as error:
        service.admit(env['owner'],[batch['runs'][0]['item_id']],'new-key-003')
    assert error.value.code=='QUOTA_EXCEEDED'


def test_concurrent_admission_atomic(env):
    items=add_demo(env);service=env['app'].state.service
    def submit(index):
        try:
            return service.admit(env['owner'],[items[0]['id']],f'concurrent-key-{index}')['id']
        except AppError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(submit,[1,2]))
    assert results.count('BUSY')==1
    assert len(env['db'].all('SELECT * FROM batches'))==1
    assert len(env['db'].all('SELECT * FROM runs'))==1


async def test_qwen_failure_no_flux_and_refunds_image_allowance(env):
    env['provider'].behavior['qwen']='failure'
    batch=start(env)
    await finish(env)
    run=env['app'].state.service.batch(env['owner'],batch['id'])['runs'][0]
    assert run['state']=='failed' and run['error']['stage']=='qwen'
    assert [stage for stage,_ in env['provider'].submissions]==['qwen']
    assert env['db'].one('SELECT reserved FROM runs')['reserved']==0


async def test_flux_failure_retry_uses_saved_prompt(env):
    env['provider'].behavior['flux']='failure'
    batch=start(env)
    await finish(env)
    old=env['app'].state.service.batch(env['owner'],batch['id'])['runs'][0]
    env['provider'].behavior.clear()
    response=env['client'].post('/api/runs/'+old['id']+'/retry?view_id='+env['view_id'],json={'idempotency_key':'image-retry-1'},headers=env['headers'])
    assert response.status_code==200
    await finish(env)
    new=env['app'].state.service.batch(env['owner'],response.json()['id'])['runs'][0]
    assert new['state']=='completed' and new['parent_run_id']==old['id']
    assert new['final_prompt']==old['final_prompt']
    assert [stage for stage,_ in env['provider'].submissions]==['qwen','flux','flux']


async def test_unknown_submission_not_resubmitted_retains_capacity(env):
    env['provider'].behavior['qwen']='unknown'
    batch=start(env)
    await finish(env)
    run=env['app'].state.service.batch(env['owner'],batch['id'])['runs'][0]
    assert run['state']=='submission_unknown'
    assert len(env['provider'].submissions)==1
    assert env['db'].one('SELECT state FROM batches')['state']=='active'
    assert env['db'].one('SELECT reserved FROM runs')['reserved']==2


async def test_crash_submitting_without_id_unknown(env):
    batch=start(env);coordinator=env['app'].state.coordinator
    env['db'].execute("UPDATE runs SET state='creating_prompt',deadline=?",(time.time()+600,))
    run=env['db'].one('SELECT * FROM runs')
    invocation=coordinator.prepare(run,'qwen')
    env['db'].execute("UPDATE invocations SET status='submitting' WHERE id=?",(invocation['id'],))
    await finish(env)
    assert env['provider'].submissions==[]
    assert env['db'].one('SELECT state FROM runs')['state']=='submission_unknown'


async def test_restart_known_id_reconciles(env):
    batch=start(env)
    await env['app'].state.coordinator.tick()
    restarted=Coordinator(env['db'],env['config'],env['provider'])
    for _ in range(4):await restarted.tick()
    assert env['db'].one('SELECT state FROM runs')['state']=='completed'
    assert len(env['provider'].submissions)==2


async def test_deadline_pending_cancellation_keeps_capacity(env):
    env['provider'].behavior['qwen']='pending'
    batch=start(env)
    await env['app'].state.coordinator.tick()
    env['db'].execute('UPDATE runs SET deadline=0')
    async def uncertain_cancel(stage,provider_id):return None
    env['provider'].cancel=uncertain_cancel
    await env['app'].state.coordinator.tick()
    assert env['db'].one('SELECT state FROM batches')['state']=='active'
    assert env['db'].one('SELECT state FROM runs')['state']=='creating_prompt'
    assert json.loads(env['db'].one('SELECT error FROM runs')['error'])['code']=='DEADLINE_EXCEEDED'
    env['provider'].behavior.clear()
    await env['app'].state.coordinator.tick()
    assert env['db'].one('SELECT state FROM batches')['state']=='finished'
    assert env['db'].one('SELECT state FROM runs')['state']=='failed'
    assert len(env['provider'].submissions)==1


async def test_missing_provider_result_does_not_invent_completion(env):
    start(env)
    await env['app'].state.coordinator.tick()
    env['provider'].jobs.clear()
    await finish(env)
    assert env['db'].one('SELECT state FROM runs')['state']=='submission_unknown'
    assert env['db'].one('SELECT state FROM batches')['state']=='active'


async def test_expired_result_preserves_successful_history(env):
    batch=start(env);await finish(env)
    run=env['app'].state.service.batch(env['owner'],batch['id'])['runs'][0]
    env['provider'].jobs.clear()
    response=env['client'].get('/api/runs/'+run['id']+'/image?view_id='+env['view_id'])
    assert response.status_code==410
    stored=env['app'].state.service.run(env['owner'],run['id'])
    assert stored['state']=='completed' and stored['availability']=='unavailable'
    assert len(env['provider'].submissions)==2


async def test_malformed_completed_payload_does_not_advance(env):
    env['provider'].behavior['qwen']='malformed'
    start(env);await finish(env)
    assert env['db'].one('SELECT state FROM runs')['state']=='failed'
    assert len(env['provider'].submissions)==1


async def test_prefix_filter_snapshot(env):
    settings=dict(DEFAULT_SETTINGS,flux_prefix='Restricted subject {{qwen_prompt}}',blocked_terms='restricted')
    env['db'].execute('UPDATE settings SET value=?',(json.dumps(settings),))
    batch=start(env)
    env['db'].execute('UPDATE settings SET value=?',(json.dumps(DEFAULT_SETTINGS),))
    await finish(env)
    run=env['app'].state.service.batch(env['owner'],batch['id'])['runs'][0]
    assert run['state']=='completed' and run['used_fallback']
    assert run['matched_terms']==['restricted'] and run['final_prompt']==settings['fallback_prompt']


async def test_unknown_success_attempt_count_retains_reservation(env):
    original_status=env['provider'].status
    async def status(stage,provider_id):
        response=await original_status(stage,provider_id)
        if stage=='flux' and response.get('output'):
            response['output']['result']['counters']['inference_attempts']=None
            response['output']['result']['counters']['inference_retries']=None
        return response
    env['provider'].status=status
    start(env);await finish(env)
    assert env['db'].one('SELECT state,reserved FROM runs')=={'state':'completed','reserved':2}


async def test_verified_result_transition_is_atomic(env):
    import sqlite3
    batch=start(env)
    for _ in range(3):await env['app'].state.coordinator.tick()
    env['db'].execute("CREATE TRIGGER simulate_crash BEFORE UPDATE OF state ON runs WHEN NEW.state='completed' BEGIN SELECT RAISE(ABORT, 'simulated crash'); END")
    with pytest.raises(sqlite3.IntegrityError):await env['app'].state.coordinator.tick()
    stored=env['db'].one("SELECT status,output FROM invocations WHERE stage='flux'")
    assert stored=={'status':'pending','output':None}
    assert env['db'].one('SELECT state,completed_at FROM runs')=={'state':'generating_image','completed_at':None}
    assert env['db'].one('SELECT state FROM batches')['state']=='active'
    env['db'].execute('DROP TRIGGER simulate_crash')
    await env['app'].state.coordinator.tick()
    assert env['db'].one('SELECT state FROM runs')['state']=='completed'
    assert env['db'].one('SELECT state FROM batches')['state']=='finished'


async def test_legacy_verified_invocation_recovers_without_provider_result(env):
    batch=start(env);await finish(env)
    run_id=batch['runs'][0]['id']
    # Simulate an interrupted older deployment that already durably verified an image.
    env['db'].execute("UPDATE runs SET state='generating_image',deadline=0 WHERE id=?",(run_id,))
    env['db'].execute("UPDATE batches SET state='active' WHERE id=?",(batch['id'],))
    env['provider'].jobs.clear()
    await env['app'].state.coordinator.tick()
    assert env['db'].one('SELECT state FROM runs')['state']=='completed'
    assert env['db'].one('SELECT state FROM batches')['state']=='finished'
    assert len(env['provider'].submissions)==2


async def test_estimated_expiry_does_not_block_existing_retrieval(env):
    batch=start(env);await finish(env)
    run_id=batch['runs'][0]['id']
    env['db'].execute('UPDATE runs SET result_expires_at=1,expiry_estimated=1 WHERE id=?',(run_id,))
    run=env['app'].state.service.run(env['owner'],run_id)
    assert run['availability']=='available_to_retrieve' and run['expiry_estimated']
    assert env['client'].get('/api/runs/'+run_id+'/image?view_id='+env['view_id']).status_code==200
    assert len(env['provider'].submissions)==2
    env['provider'].jobs.clear()
    assert env['client'].get('/api/runs/'+run_id+'/image?view_id='+env['view_id']).status_code==410
    assert env['app'].state.service.run(env['owner'],run_id)['availability']=='unavailable'
