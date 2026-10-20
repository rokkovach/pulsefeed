import os
import subprocess
import sys
from pulsefeed.auth import token_hash
from conftest import start


def command(env,*args):
    config=env['config']
    variables=os.environ|{'DATABASE_PATH':config.database_path,'SESSION_SECRET':config.session_secret,'PULSEFEED_FAKE_MODE':'true','COOKIE_SECURE':'false','APP_BASE_URL':config.base_url}
    return subprocess.run([sys.executable,'-m','pulsefeed.cli',*args],env=variables,capture_output=True,text=True,check=True).stdout.strip()


def test_cli_seed_issue_and_revoke(env):
    account=command(env,'seed','--name','new-evaluator','--role','evaluator')
    link=command(env,'issue-link','--account','new-evaluator')
    response=env['client'].get(link.replace(env['config'].base_url,''),follow_redirects=False)
    assert response.status_code==303
    assert command(env,'revoke','--account',account)=='Access revoked'
    assert env['client'].get('/api/session').status_code==401
    assert env['db'].one('SELECT revoked FROM sessions WHERE account_id=?',(account,))['revoked']==1


async def test_cli_confirm_stopped_retains_unknown_quota(env):
    env['provider'].behavior['qwen']='unknown'
    batch=start(env)
    await env['app'].state.coordinator.tick()
    run_id=batch['runs'][0]['id']
    command(env,'resolve','--run',run_id,'--confirm-stopped')
    assert env['db'].one('SELECT state,reserved FROM runs')=={'state':'failed','reserved':2}
    assert env['db'].one('SELECT state FROM batches')['state']=='finished'
