"""Preset changes must survive admission, retries and provider image validation."""
import pytest

from pulsefeed.config import Config
from conftest import start


@pytest.mark.parametrize('size', [256,512,1024])
async def test_new_runs_use_saved_dimensions_through_pipeline(env, size):
    env['config'].flux_image_size = size
    start(env, key='size-test-00001')
    with env['db'].connect() as conn:
        run = conn.execute('SELECT * FROM runs').fetchone()
    assert run['width'] == run['height'] == size
    # Changing the deployment default must not change this admitted job.
    env['config'].flux_image_size = 1024 if size != 1024 else 256
    for _ in range(8):
        await env['app'].state.coordinator.tick()
    with env['db'].connect() as conn:
        invocation = conn.execute("SELECT * FROM invocations WHERE stage='flux'").fetchone()
        run = conn.execute('SELECT * FROM runs').fetchone()
    import json
    payload = json.loads(invocation['input'])
    assert payload['width'] == payload['height'] == size
    assert run['state'] == 'completed'
    env['app'].state.service.retry_view(env['owner'], env['view_id'], run['id'], 'size-retry-0001')
    with env['db'].connect() as conn:
        retry = conn.execute('SELECT * FROM runs WHERE parent_run_id=?', (run['id'],)).fetchone()
    assert retry['width'] == retry['height'] == size


@pytest.mark.parametrize('size', [200,2048,True,512.0])
def test_invalid_image_size_configuration_is_rejected(size):
    config = Config(session_secret='s'*40, fake_mode=True, flux_image_size=size)
    with pytest.raises(ValueError, match='FLUX_IMAGE_SIZE'):
        config.validate()


@pytest.mark.parametrize('size', [256,512,1024])
def test_environment_selects_supported_preset(monkeypatch, size):
    monkeypatch.setenv('SESSION_SECRET', 's'*40)
    monkeypatch.setenv('PULSEFEED_FAKE_MODE', 'true')
    monkeypatch.setenv('FLUX_IMAGE_SIZE', str(size))
    assert Config.from_env().flux_image_size == size
