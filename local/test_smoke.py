"""Readiness transport regressions; no sockets, models or downloads."""
from http.client import RemoteDisconnected
from urllib.error import HTTPError

import pytest

from local import smoke


@pytest.fixture
def clock(monkeypatch):
    state={'now':0,'sleeps':[]}
    monkeypatch.setattr(smoke.time,'monotonic',lambda:state['now'])
    def sleep(seconds):
        state['sleeps'].append(seconds)
        state['now']+=seconds
    monkeypatch.setattr(smoke.time,'sleep',sleep)
    return state


def test_readiness_disconnect_and_reset_then_ready(monkeypatch,clock):
    endpoint=smoke.Endpoint('http://127.0.0.1:8182','synthetic-token')
    replies=iter([RemoteDisconnected('not bound yet'),ConnectionResetError('starting'),{'status':'ready'}])
    calls=[]
    def request(method,path):
        calls.append((method,path))
        value=next(replies)
        if isinstance(value,Exception):raise value
        return value
    monkeypatch.setattr(endpoint,'request',request)
    endpoint.ready(9)
    assert calls==[('GET','/health')]*3 and clock['sleeps']==[3,3]


@pytest.mark.parametrize('error',[RemoteDisconnected,ConnectionResetError])
def test_readiness_reset_stops_at_existing_deadline(monkeypatch,clock,error):
    endpoint=smoke.Endpoint('http://127.0.0.1:8182','synthetic-token')
    calls=[]
    def request(method,path):
        calls.append((method,path))
        raise error('starting')
    monkeypatch.setattr(endpoint,'request',request)
    with pytest.raises(RuntimeError,match='Timed out waiting'):
        endpoint.ready(6)
    assert clock['now']==6 and len(calls)==3 and clock['sleeps']==[3,3]


@pytest.mark.parametrize('status',[401,403,404,429,500])
def test_readiness_nonstartup_http_errors_not_swallowed(monkeypatch,clock,status):
    endpoint=smoke.Endpoint('http://127.0.0.1:8182','synthetic-token')
    def request(method,path):raise HTTPError('http://127.0.0.1/health',status,'rejected',{},None)
    monkeypatch.setattr(endpoint,'request',request)
    with pytest.raises(RuntimeError,match=f'Readiness rejected with HTTP {status}'):
        endpoint.ready(60)
    assert clock['sleeps']==[]


def test_readiness_unexpected_payload_not_swallowed(monkeypatch,clock):
    endpoint=smoke.Endpoint('http://127.0.0.1:8182','synthetic-token')
    monkeypatch.setattr(endpoint,'request',lambda *args:{'status':'broken'})
    with pytest.raises(RuntimeError,match='Unexpected health response'):
        endpoint.ready(60)
    assert clock['sleeps']==[]


@pytest.mark.parametrize('error',[RemoteDisconnected,ConnectionResetError])
def test_submission_disconnect_is_not_retried(monkeypatch,error):
    endpoint=smoke.Endpoint('http://127.0.0.1:8182','synthetic-token')
    calls=[]
    def request(method,path,payload=None):
        calls.append((method,path))
        raise error('acceptance unknown')
    monkeypatch.setattr(endpoint,'request',request)
    with pytest.raises(error):endpoint.invoke({'deadline_unix_ms':1})
    assert calls==[('POST','/run')]
