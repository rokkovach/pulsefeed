"""Bounded request overlap with durable invocation state and synthetic providers."""
import asyncio
import json
import time

import pytest

from conftest import start
from pulsefeed.config import Config
from pulsefeed.coordinator import Coordinator
from test_jobs import finish
from test_view_feed import source


def runs(env):
    return env['db'].all('SELECT * FROM runs ORDER BY ordinal')


def pending(env):
    return env['db'].all("SELECT * FROM invocations WHERE status IN ('prepared','submitting','pending','cancelling')")


async def overlap_ready(env):
    start(env,count=3)
    coordinator=env['app'].state.coordinator
    await coordinator.tick()  # Qwen0 accepted.
    await coordinator.tick()  # Qwen0 done, ready for FLUX0 + Qwen1.
    return coordinator


async def test_distinct_stage_submissions_overlap_but_same_stage_and_started_items_stay_bounded(env):
    coordinator=await overlap_ready(env)
    submit=env['provider'].submit
    arrived=set();both=asyncio.Event()
    async def barrier_submit(stage,payload):
        arrived.add(stage)
        if len(arrived)==2:both.set()
        await asyncio.wait_for(both.wait(),1)
        return await submit(stage,payload)
    env['provider'].submit=barrier_submit
    await asyncio.wait_for(coordinator.tick(),2)
    assert arrived=={'flux','qwen'}
    assert [row['stage'] for row in pending(env)]==['flux','qwen']
    assert len([run for run in runs(env) if run['state']!='queued'])==2
    assert runs(env)[2]['deadline'] is None
    env['provider'].submit=submit
    for _ in range(20):
        await coordinator.tick()
        active=[run for run in runs(env) if run['state'] not in ('queued','completed','failed')]
        assert len(active)<=2
        stages=[row['stage'] for row in pending(env)]
        assert stages.count('qwen')<=1 and stages.count('flux')<=1
    assert [run['state'] for run in runs(env)]==['completed']*3
    assert [payload['run_id'] for stage,payload in env['provider'].submissions if stage=='flux']==[run['id'] for run in runs(env)]
    assert len(env['provider'].submissions)==6


async def test_fast_qwen_and_slow_flux_do_not_start_a_third_item_or_submit_next_flux(env):
    coordinator=await overlap_ready(env)
    env['provider'].behavior['flux']='pending'
    await coordinator.tick()
    await coordinator.tick()  # Qwen1 finishes while FLUX0 remains accepted.
    second=runs(env)[1]
    prepared=coordinator.prepare(second,'flux')
    # An already prepared next-stage request is also held behind the accepted
    # same-stage invocation, including after restart.
    coordinator=Coordinator(env['db'],env['config'],env['provider'])
    for _ in range(5):await coordinator.tick()
    assert [run['state'] for run in runs(env)]==['generating_image','generating_image','queued']
    assert runs(env)[2]['deadline'] is None
    assert [stage for stage,_ in env['provider'].submissions]==['qwen','flux','qwen']
    assert env['db'].one('SELECT status FROM invocations WHERE id=?',(prepared['id'],))['status']=='prepared'
    env['provider'].behavior.clear()
    await coordinator.tick()  # FLUX0 terminal; no duplicate dispatch in the same wave.
    await coordinator.tick()  # Prepared FLUX1 reused alongside Qwen2.
    assert len([payload for stage,payload in env['provider'].submissions if stage=='flux'])==2
    assert [payload['invocation_id'] for stage,payload in env['provider'].submissions if stage=='flux'][-1]==prepared['id']
    await finish(env)
    assert [run['state'] for run in runs(env)]==['completed']*3
    assert len(env['db'].all('SELECT id FROM invocations'))==6


async def test_expired_qwen_complete_item_waiting_for_flux_slot_refunds_unsent_image_attempts(env):
    coordinator=await overlap_ready(env)
    env['provider'].behavior['flux']='pending'
    await coordinator.tick()  # FLUX0 accepted alongside Qwen1.
    await coordinator.tick()  # Qwen1 done, waiting behind FLUX0.
    second=runs(env)[1]
    assert second['state']=='generating_image'
    assert not env['db'].one("SELECT id FROM invocations WHERE run_id=? AND stage='flux'",(second['id'],))
    env['db'].execute('UPDATE runs SET deadline=0 WHERE id=?',(second['id'],))
    env['provider'].behavior.clear()
    await coordinator.tick()  # Observe FLUX0 completion before reusing the stage.
    await coordinator.tick()  # Expire run1 before preparing or submitting FLUX.
    failed=env['db'].one('SELECT * FROM runs WHERE id=?',(second['id'],))
    assert failed['state']=='failed' and failed['reserved']==0
    assert json.loads(failed['error'])['code']=='DEADLINE_EXCEEDED'
    assert not env['db'].one("SELECT id FROM invocations WHERE run_id=? AND stage='flux'",(second['id'],))
    assert [payload['run_id'] for stage,payload in env['provider'].submissions if stage=='flux']==[runs(env)[0]['id']]
    assert env['app'].state.service.quota_summary()['global']['charged']==3  # Image0 + run2 reservation.


async def test_serial_switch_does_not_start_next_item_before_previous_image_finishes(env):
    env['config'].coordinator_pipeline_size=1
    start(env,count=3)
    coordinator=env['app'].state.coordinator
    for _ in range(3):await coordinator.tick()
    assert [run['state'] for run in runs(env)]==['generating_image','queued','queued']
    assert all(run['deadline'] is None for run in runs(env)[1:])
    await finish(env)
    assert [stage for stage,_ in env['provider'].submissions]==['qwen','flux']*3


async def test_unknown_flux_submission_freezes_queued_start_in_same_wave(env):
    coordinator=await overlap_ready(env)
    env['provider'].behavior['flux']='unknown'
    await coordinator.tick()
    assert [run['state'] for run in runs(env)]==['submission_unknown','queued','queued']
    assert all(run['deadline'] is None for run in runs(env)[1:])
    for _ in range(3):await coordinator.tick()
    assert [stage for stage,_ in env['provider'].submissions]==['qwen','flux']
    assert env['db'].one('SELECT state FROM batches')['state']=='active'


async def test_unknown_next_qwen_freezes_new_dispatch_but_known_flux_drains(env):
    coordinator=await overlap_ready(env)
    env['provider'].behavior['qwen']='unknown'
    await coordinator.tick()
    assert [run['state'] for run in runs(env)]==['generating_image','submission_unknown','queued']
    await coordinator.tick()
    assert [run['state'] for run in runs(env)]==['completed','submission_unknown','queued']
    assert runs(env)[2]['deadline'] is None
    assert len(env['provider'].submissions)==3
    assert env['app'].state.service.quota_summary()['global']['charged']==5


async def test_restart_acceptance_gap_is_frozen_before_new_flux_dispatch(env):
    coordinator=await overlap_ready(env)
    second=runs(env)[1]
    env['db'].execute("UPDATE runs SET state='creating_prompt',deadline=? WHERE id=?",(time.time()+600,second['id']))
    second=env['db'].one('SELECT * FROM runs WHERE id=?',(second['id'],))
    invocation=coordinator.prepare(second,'qwen')
    env['db'].execute("UPDATE invocations SET status='submitting' WHERE id=?",(invocation['id'],))
    restarted=Coordinator(env['db'],env['config'],env['provider'])
    await restarted.tick()
    assert [run['state'] for run in runs(env)]==['generating_image','submission_unknown','queued']
    assert len(env['provider'].submissions)==1
    assert not env['db'].one("SELECT * FROM invocations WHERE stage='flux'")


async def test_restart_downgrade_drains_both_accepted_stages_before_starting_more(env):
    coordinator=await overlap_ready(env)
    await coordinator.tick()
    original_ids=[row['id'] for row in env['db'].all('SELECT id FROM invocations')]
    env['config'].coordinator_pipeline_size=1
    restarted=Coordinator(env['db'],env['config'],env['provider'])
    await restarted.tick()
    assert [run['state'] for run in runs(env)]==['completed','generating_image','queued']
    assert runs(env)[2]['deadline'] is None
    await restarted.tick()
    assert runs(env)[2]['deadline'] is None
    await restarted.tick()
    assert runs(env)[1]['state']=='completed'
    await restarted.tick()
    assert runs(env)[2]['state']=='creating_prompt'
    assert all(env['db'].one('SELECT id FROM invocations WHERE id=?',(identity,)) for identity in original_ids)
    assert len(env['provider'].submissions)==5


async def test_concurrent_ticks_use_one_locked_dispatch_wave_and_no_duplicate_invocations(env):
    coordinator=await overlap_ready(env)
    await asyncio.gather(*(coordinator.tick() for _ in range(5)))
    await finish(env)
    invocations=env['db'].all('SELECT run_id,stage,id FROM invocations')
    assert len(invocations)==6
    assert len({(row['run_id'],row['stage']) for row in invocations})==6
    assert len({payload['invocation_id'] for _,payload in env['provider'].submissions})==6


async def test_deadline_cancellation_drains_both_stages_without_starting_third(env):
    coordinator=await overlap_ready(env)
    await coordinator.tick()
    env['provider'].behavior.update(qwen='pending',flux='pending')
    env['db'].execute("UPDATE runs SET deadline=0 WHERE state!='queued'")
    cancelled=[]
    async def uncertain_cancel(stage,provider_id):cancelled.append(stage)
    env['provider'].cancel=uncertain_cancel
    await coordinator.tick()
    assert set(cancelled)=={'qwen','flux'}
    assert [run['state'] for run in runs(env)]==['generating_image','creating_prompt','queued']
    assert runs(env)[2]['deadline'] is None
    coordinator=Coordinator(env['db'],env['config'],env['provider'])
    await coordinator.tick()
    assert len(cancelled)==2  # Never repeatedly issue cancellation, including restart.
    env['provider'].behavior.clear()
    await coordinator.tick()
    assert [run['state'] for run in runs(env)]==['failed','failed','queued']
    assert all(json.loads(run['error'])['code']=='DEADLINE_EXCEEDED' for run in runs(env)[:2])
    assert len(env['provider'].submissions)==3


async def test_cancellation_waits_for_all_stage_tasks_and_restart_does_not_resubmit(env):
    coordinator=await overlap_ready(env)
    entered=set();ready=asyncio.Event();finished=set()
    async def waiting_submit(stage,payload):
        entered.add(stage)
        if len(entered)==2:ready.set()
        try:await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            finished.add(stage)
    env['provider'].submit=waiting_submit
    tick=asyncio.create_task(coordinator.tick())
    await asyncio.wait_for(ready.wait(),1)
    tick.cancel()
    with pytest.raises(asyncio.CancelledError):await tick
    assert finished=={'flux','qwen'} and not coordinator.lock.locked()
    assert len(env['db'].all("SELECT * FROM invocations WHERE status='submitting'"))==2
    restarted=Coordinator(env['db'],env['config'],env['provider'])
    await restarted.tick()
    assert [run['state'] for run in runs(env)]==['submission_unknown','submission_unknown','queued']
    assert len(env['provider'].submissions)==1


async def test_one_stage_exception_waits_for_other_stage_before_unlock_and_reconciliation(env):
    coordinator=await overlap_ready(env)
    original=env['provider'].submit
    other_finished=asyncio.Event();other_started=asyncio.Event()
    async def submit(stage,payload):
        if stage=='flux':
            await other_started.wait()
            raise RuntimeError('synthetic transport failure')
        other_started.set()
        await asyncio.sleep(0.01)
        result=await original(stage,payload)
        other_finished.set()
        return result
    env['provider'].submit=submit
    with pytest.raises(RuntimeError):await coordinator.tick()
    assert other_finished.is_set() and not coordinator.lock.locked()
    assert env['db'].one("SELECT status FROM invocations WHERE stage='flux'")['status']=='unknown'
    await coordinator.tick()
    assert runs(env)[0]['state']=='submission_unknown'
    assert runs(env)[1]['state']=='generating_image'  # Known Qwen result drained.
    assert not env['db'].one("SELECT id FROM invocations WHERE stage='flux' AND run_id=?",(runs(env)[1]['id'],))
    assert len(env['provider'].submissions)==2


async def test_unexpected_submit_failure_freezes_sibling_that_has_not_dispatched_yet(env):
    coordinator=await overlap_ready(env)
    original=env['provider'].submit
    async def submit(stage,payload):
        if stage=='flux':raise RuntimeError('synthetic acceptance uncertainty')
        return await original(stage,payload)
    env['provider'].submit=submit
    with pytest.raises(RuntimeError):await coordinator.tick()
    assert [run['state'] for run in runs(env)]==['submission_unknown','queued','queued']
    assert all(run['deadline'] is None for run in runs(env)[1:])
    assert len(env['provider'].submissions)==1


@pytest.mark.parametrize('size',[0,3,True,1.0])
def test_pipeline_config_rejects_unbounded_or_noninteger_values(size):
    with pytest.raises(ValueError,match='COORDINATOR_PIPELINE_SIZE'):
        Config(session_secret='x'*40,fake_mode=True,coordinator_pipeline_size=size).validate()


def test_pipeline_config_environment_and_serial_rollback(monkeypatch):
    monkeypatch.setenv('SESSION_SECRET','x'*40)
    monkeypatch.setenv('PULSEFEED_FAKE_MODE','true')
    monkeypatch.delenv('COORDINATOR_PIPELINE_SIZE',raising=False)
    assert Config.from_env().coordinator_pipeline_size==2
    monkeypatch.setenv('COORDINATOR_PIPELINE_SIZE','1')
    assert Config.from_env().coordinator_pipeline_size==1
