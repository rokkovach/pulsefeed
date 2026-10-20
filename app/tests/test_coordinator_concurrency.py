"""Bounded replicas must not weaken durable dispatch, quota or restart handling."""
import asyncio

import pytest

from pulsefeed.config import Config
from pulsefeed.coordinator import Coordinator
from test_jobs import finish
from test_view_feed import source


def admit_nine(env, concurrency=3):
    config = env['config']
    config.coordinator_stage_concurrency = concurrency
    config.coordinator_pipeline_size = 2*concurrency
    config.validate()
    result = env['app'].state.service.view_feed(
        env['owner'], 'demo', 'parallel-nine-0001', source(tuple(f'story-{i}' for i in range(9))))
    assert result['generation']['status'] == 'started'
    assert len(result['runs']) == 9
    return env['app'].state.coordinator


def runs(env):
    return env['db'].all('SELECT * FROM runs ORDER BY ordinal')


def pending(env):
    return env['db'].all("SELECT * FROM invocations WHERE status IN ('prepared','submitting','pending','cancelling')")


async def ready_for_six(env):
    coordinator = admit_nine(env)
    await coordinator.tick()  # Three Qwen jobs accepted.
    await coordinator.tick()  # Their prompts complete.
    return coordinator


async def test_three_jobs_per_model_overlap_but_no_seventh_story_starts(env):
    coordinator = await ready_for_six(env)
    original = env['provider'].submit
    entered = []
    all_entered = asyncio.Event()
    env['provider'].behavior.update(qwen='pending', flux='pending')

    async def submit(stage, payload):
        entered.append(stage)
        if len(entered) == 6:
            all_entered.set()
        await asyncio.wait_for(all_entered.wait(), 1)
        return await original(stage, payload)

    env['provider'].submit = submit
    await asyncio.wait_for(coordinator.tick(), 2)
    assert entered.count('flux') == entered.count('qwen') == 3
    assert len(pending(env)) == 6
    assert len([run for run in runs(env) if run['state'] != 'queued']) == 6
    assert all(run['deadline'] is None for run in runs(env)[6:])
    await coordinator.tick()
    assert len(env['provider'].submissions) == 9  # Three earlier + six held jobs.
    assert env['app'].state.service.quota_summary()['global']['charged'] == 18


@pytest.mark.parametrize('limit', [1, 2, 3])
async def test_complete_nine_exactly_once_with_stage_and_quota_bounds(env, limit):
    coordinator = admit_nine(env, limit)
    for _ in range(64):
        await coordinator.tick()
        active = [run for run in runs(env) if run['state'] not in ('queued','completed','failed')]
        assert len(active) <= 2*limit
        stages = [row['stage'] for row in pending(env)]
        assert stages.count('qwen') <= limit and stages.count('flux') <= limit
        if all(run['state'] == 'completed' for run in runs(env)):
            break
    assert [run['state'] for run in runs(env)] == ['completed']*9
    invocations = env['db'].all('SELECT run_id,stage FROM invocations')
    assert len(invocations) == len({(row['run_id'],row['stage']) for row in invocations}) == 18
    assert len(env['provider'].submissions) == 18
    quota = env['app'].state.service.quota_summary()
    assert quota['global']['charged'] == quota['global']['used'] == 9
    assert quota['global']['reserved'] == 0
    assert quota['hourly']['used'] == 9


async def test_lower_limit_after_restart_polls_all_six_accepted_jobs(env):
    coordinator = await ready_for_six(env)
    env['provider'].behavior.update(qwen='pending', flux='pending')
    await coordinator.tick()
    original_ids = {row['provider_id'] for row in pending(env)}
    assert len(original_ids) == 6
    status = env['provider'].status
    polled = []

    async def record_status(stage, provider_id):
        polled.append(provider_id)
        return await status(stage, provider_id)

    env['provider'].status = record_status
    env['config'].coordinator_stage_concurrency = 1
    env['config'].coordinator_pipeline_size = 2
    restarted = Coordinator(env['db'],env['config'],env['provider'])
    await restarted.tick()
    assert set(polled) == original_ids
    assert len(env['provider'].submissions) == 9
    assert all(run['deadline'] is None for run in runs(env)[6:])
    assert env['app'].state.service.quota_summary()['global']['charged'] == 18
    env['provider'].behavior.clear()
    env['app'].state.coordinator = restarted
    await finish(env)
    assert [run['state'] for run in runs(env)] == ['completed']*9
    assert len(env['provider'].submissions) == 18


async def test_immediate_ambiguous_submit_freezes_other_new_dispatches(env):
    coordinator = admit_nine(env)
    env['provider'].behavior['qwen'] = 'unknown'
    await coordinator.tick()
    assert runs(env)[0]['state'] == 'submission_unknown'
    assert all(run['state'] == 'queued' and run['deadline'] is None for run in runs(env)[1:])
    await coordinator.tick()
    assert len(env['provider'].submissions) == 1
    assert env['app'].state.service.quota_summary()['global']['charged'] == 18


async def test_missing_result_freezes_dispatch_but_known_siblings_drain(env):
    coordinator = admit_nine(env)
    await coordinator.tick()
    missing = pending(env)[0]['provider_id']
    env['provider'].jobs.pop(missing)
    await coordinator.tick()
    await coordinator.tick()
    assert runs(env)[0]['state'] == 'submission_unknown'
    assert [run['state'] for run in runs(env)[1:3]] == ['generating_image']*2
    assert len(env['provider'].submissions) == 3
    assert all(run['deadline'] is None for run in runs(env)[3:])


async def test_cancelled_six_task_wave_drains_before_unlock_and_never_resubmits(env):
    coordinator = await ready_for_six(env)
    entered = set()
    finished = set()
    ready = asyncio.Event()

    async def blocked_submit(stage, payload):
        identity = payload['invocation_id']
        entered.add(identity)
        if len(entered) == 6:
            ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            finished.add(identity)

    env['provider'].submit = blocked_submit
    tick = asyncio.create_task(coordinator.tick())
    await asyncio.wait_for(ready.wait(), 1)
    tick.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tick
    assert finished == entered and not coordinator.lock.locked()
    assert len(env['db'].all("SELECT id FROM invocations WHERE status='submitting'")) == 6
    restarted = Coordinator(env['db'],env['config'],env['provider'])
    await restarted.tick()
    assert [run['state'] for run in runs(env)[:6]] == ['submission_unknown']*6
    assert all(run['deadline'] is None for run in runs(env)[6:])
    assert len(env['provider'].submissions) == 3
    assert env['app'].state.service.quota_summary()['global']['charged'] == 18


async def test_concurrent_ticks_do_not_duplicate_any_of_nine_jobs(env):
    coordinator = admit_nine(env)
    for _ in range(4):
        await asyncio.gather(*(coordinator.tick() for _ in range(4)))
    await finish(env)
    pairs = [(payload['run_id'],stage) for stage,payload in env['provider'].submissions]
    assert len(pairs) == len(set(pairs)) == 18


@pytest.mark.parametrize('limit', [0,4,True,1.0])
def test_stage_concurrency_is_a_bounded_integer(limit):
    with pytest.raises(ValueError,match='COORDINATOR_STAGE_CONCURRENCY'):
        Config(session_secret='s'*40,fake_mode=True,coordinator_stage_concurrency=limit).validate()


def test_environment_sets_three_per_model_and_six_total_unless_overridden(monkeypatch):
    monkeypatch.setenv('SESSION_SECRET','s'*40)
    monkeypatch.setenv('PULSEFEED_FAKE_MODE','true')
    monkeypatch.setenv('COORDINATOR_STAGE_CONCURRENCY','3')
    monkeypatch.delenv('COORDINATOR_PIPELINE_SIZE',raising=False)
    config = Config.from_env()
    assert config.coordinator_stage_concurrency == 3 and config.coordinator_pipeline_size == 6
    monkeypatch.setenv('COORDINATOR_PIPELINE_SIZE','3')
    assert Config.from_env().coordinator_pipeline_size == 3


async def test_six_deadline_cancellations_are_sent_once_and_reservations_wait_for_results(env):
    coordinator = await ready_for_six(env)
    env['provider'].behavior.update(qwen='pending', flux='pending')
    await coordinator.tick()
    env['db'].execute("UPDATE runs SET deadline=0 WHERE state!='queued'")
    cancelled = []

    async def uncertain_cancel(stage, provider_id):
        cancelled.append(provider_id)

    env['provider'].cancel = uncertain_cancel
    await coordinator.tick()
    assert len(cancelled) == len(set(cancelled)) == 6
    assert env['app'].state.service.quota_summary()['global']['charged'] == 18
    restarted = Coordinator(env['db'],env['config'],env['provider'])
    await restarted.tick()
    assert len(cancelled) == 6
    env['provider'].behavior.clear()
    await restarted.tick()
    assert [run['state'] for run in runs(env)[:6]] == ['failed']*6
    assert all(run['deadline'] is None for run in runs(env)[6:])
    # Three late FLUX attempts stay charged; the unsent Qwen image stages
    # release their reservations, and the final three queued stories retain six.
    assert env['app'].state.service.quota_summary()['global']['charged'] == 9
