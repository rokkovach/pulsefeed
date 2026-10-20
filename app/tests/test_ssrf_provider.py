import asyncio
import base64
import copy
import io
import json
import pytest
import httpx
from PIL import Image
from pulsefeed.errors import AppError
from pulsefeed.rss import public_url,validate_address,fetch_bytes,parse_feed,normalize,PinnedConnection
from pulsefeed.provider import validate_envelope,RunpodAdapter,ReadUnavailable,SubmissionUnknown
from conftest import start


@pytest.mark.parametrize('address',['127.0.0.1','10.0.0.1','169.254.169.254','192.168.1.1','::1','fc00::1','fe80::1','::ffff:127.0.0.1','0.0.0.0','224.0.0.1'])
def test_ssrf_addresses(address):
    with pytest.raises(AppError):validate_address(address)


@pytest.mark.parametrize('url',['file:///etc/passwd','http://user:pass@example.com/','http://example.com:8000/','ftp://example.com/','http://example.com:bad/'])
def test_feed_url_rejections(url):
    with pytest.raises(AppError):public_url(url)


class FakeResponse:
    status=200
    def __init__(self,body=b'<rss/>',headers=None):self.body=body;self.headers=headers or {}
    def getheader(self,key,default=None):return self.headers.get(key,default)
    def read(self,size):
        result,self.body=self.body[:size],self.body[size:]
        return result


class FakeConnection:
    responses=[]
    sock=None
    def __init__(self,*args):pass
    def request(self,*args,**kwargs):pass
    def getresponse(self):return self.responses.pop(0)
    def close(self):pass


def test_redirect_dns_validation_and_response_cap():
    redirect=FakeResponse(headers={'Location':'http://metadata.example/rss'});redirect.status=302
    FakeConnection.responses=[redirect]
    resolver=lambda host,port:['169.254.169.254'] if host=='metadata.example' else ['93.184.216.34']
    with pytest.raises(AppError) as error:fetch_bytes('https://example.com/rss',resolver,FakeConnection)
    assert error.value.code=='UNSAFE_FEED'
    FakeConnection.responses=[FakeResponse(headers={'Content-Length':'2097153'})]
    with pytest.raises(AppError) as error:fetch_bytes('https://example.com/rss',lambda *_:['93.184.216.34'],FakeConnection)
    assert error.value.code=='FEED_TOO_LARGE'
    FakeConnection.responses=[FakeResponse(body=b'x'*2097153)]
    with pytest.raises(AppError):fetch_bytes('https://example.com/rss',lambda *_:['93.184.216.34'],FakeConnection)


def test_rebinding_connection_peer_rejected(monkeypatch):
    class Socket:
        closed=False
        def getpeername(self):return ('127.0.0.1',80)
        def close(self):self.closed=True
    sock=Socket()
    monkeypatch.setattr('pulsefeed.rss.socket.create_connection',lambda *args:sock)
    connection=PinnedConnection('example.com',80,'93.184.216.34',False,10)
    with pytest.raises(AppError):connection.connect()
    assert sock.closed


def test_xml_entities_and_html():
    payload=b'<!DOCTYPE rss [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss><channel><title>&x;</title></channel></rss>'
    with pytest.raises(AppError):parse_feed(payload,'https://example.com/feed')
    payload=b'<rss><channel><title>Feed</title><item><title>&lt;script&gt;evil&lt;/script&gt;Hello</title><description>&lt;b&gt;Story&lt;/b&gt;</description><link>javascript:alert(1)</link></item></channel></rss>'
    item=parse_feed(payload,'https://example.com/feed')['items'][0]
    assert item['title']=='Hello' and item['excerpt']=='Story' and item['link']==''
    assert normalize('  Cafe\u0301\n data  ')=='Café data'


async def synthetic_output(env,stage='flux'):
    payload={'schema_version':1,'stage':stage,'run_id':'run','invocation_id':'invocation','deadline_unix_ms':9999999999999}
    if stage=='flux':payload.update(prompt='a garden',seed=2,width=1024,height=1024,num_inference_steps=4,guidance_scale=1.0,output_format='webp')
    else:payload.update(article={'title':'Garden','excerpt':''},system_prompt='visual prompt',max_new_tokens=256)
    provider=env['provider'];job=await provider.submit(stage,payload)
    output=(await provider.status(stage,job))['output']['result']
    return output,{'stage':stage,'run_id':'run','id':'invocation'},payload


@pytest.mark.parametrize('mutation',['false-success','ids','zero-attempts','base64','size','width','mime','seed','unknown-field','total','counters'])
async def test_worker_validation(env,mutation):
    stage='qwen' if mutation=='total' else 'flux'
    output,invocation,payload=await synthetic_output(env,stage)
    if mutation=='false-success':output['ok']=False
    elif mutation=='ids':output['run_id']='other'
    elif mutation=='zero-attempts':output['counters']['inference_attempts']=0
    elif mutation=='base64':output['data']['image_base64']='!invalid!'
    elif mutation=='size':output['data']['image_bytes']=2097153
    elif mutation=='width':output['data']['width']=512
    elif mutation=='mime':output['data']['mime_type']='image/png'
    elif mutation=='seed':output['data']['seed']=1
    elif mutation=='unknown-field':output['secret']='hidden'
    elif mutation=='total':output['usage']={'input_tokens':2,'output_tokens':3,'total_tokens':8}
    elif mutation=='counters':output['counters']['inference_attempts']=3
    with pytest.raises(AppError):validate_envelope(output,invocation,payload)


async def test_actual_image_dimensions_not_just_metadata(env):
    output,invocation,payload=await synthetic_output(env)
    buffer=io.BytesIO();Image.new('RGB',(512,512)).save(buffer,format='WEBP')
    output['data']['image_base64']=base64.b64encode(buffer.getvalue()).decode();output['data']['image_bytes']=len(buffer.getvalue())
    with pytest.raises(AppError):validate_envelope(output,invocation,payload)


async def test_status_transport_retry_cap_and_submission_no_replay(env):
    count=0
    async def transport(request):
        nonlocal count
        count+=1
        return httpx.Response(503,request=request)
    delays=[]
    async def sleep(delay):delays.append(delay)
    adapter=RunpodAdapter(env['config'],httpx.AsyncClient(transport=httpx.MockTransport(transport)),sleep)
    with pytest.raises(ReadUnavailable):await adapter.status('qwen','job')
    assert count==4 and len(delays)==3
    count=0;delays.clear()
    with pytest.raises(SubmissionUnknown):await adapter.submit('qwen',{})
    assert count==1 and delays==[]
    await adapter.close()


async def test_retry_after_and_terminal_provider_failure(env):
    count=0
    async def transport(request):
        nonlocal count
        count+=1
        return httpx.Response(429,headers={'Retry-After':'3'},request=request) if count==1 else httpx.Response(200,json={'status':'FAILED'},request=request)
    delays=[]
    async def sleep(delay):delays.append(delay)
    adapter=RunpodAdapter(env['config'],httpx.AsyncClient(transport=httpx.MockTransport(transport)),sleep)
    assert (await adapter.status('qwen','job'))['status']=='FAILED'
    assert delays==[3]
    await adapter.close()


def test_contract_provider_wrapper_is_required():
    from pulsefeed.provider import unwrap_output
    with pytest.raises(AppError):unwrap_output({'ok':True})
    with pytest.raises(AppError):unwrap_output({'result':{},'error':None})
    assert unwrap_output({'result':{'ok':False}})=={'ok':False}


def test_parent_fixtures_are_parsed():
    from pathlib import Path
    fixture_root=Path(__file__).resolve().parents[2]/'contracts'/'fixtures'
    if not fixture_root.exists():pytest.skip('Lane-only checkout does not include shared fixtures')
    for stage in ('qwen','flux'):
        output=json.loads((fixture_root/f'{stage}-success.json').read_text())
        invocation={'id':output['invocation_id'],'run_id':output['run_id'],'stage':stage}
        request={'width':1024,'height':1024,'seed':42,'output_format':'webp'}
        assert validate_envelope(output,invocation,request) is None if stage=='qwen' else validate_envelope(output,invocation,request)


def test_truncated_article_remains_normalized_and_bounded():
    content=('<rss><channel><title>Feed</title><item><title>TT</title><description>'+'a '*3000+'</description></item></channel></rss>').encode()
    article=parse_feed(content,'https://example.com/feed')['items'][0]['article']
    assert article['excerpt']==normalize(article['excerpt'])
    assert len(article['title'])+len(article['excerpt'])<=4000
    content=('<rss><channel><title>Feed</title><item><title>'+'a '*3000+'</title><description>text</description></item></channel></rss>').encode()
    item=parse_feed(content,'https://example.com/feed')['items'][0]
    assert len(item['title'])<=500  # Bound the saved source fields as well as the model snapshot.
    article=item['article']
    assert article['title']==normalize(article['title'])
    assert len(article['title'])+len(article['excerpt'])<=4000


@pytest.mark.parametrize('counters',[{'inference_attempts':1,'inference_retries':0,'dependency_retries':2},{'inference_attempts':2,'inference_retries':1,'dependency_retries':1}])
async def test_shared_worker_retry_budget(env,counters):
    output,invocation,payload=await synthetic_output(env)
    output['counters']=counters
    with pytest.raises(AppError):validate_envelope(output,invocation,payload)


async def test_error_taxonomy_is_stable(env):
    output,invocation,payload=await synthetic_output(env)
    output.update(ok=False,data=None,error={'code':'UNDOCUMENTED','message':'Nope','retryable':False},usage={'image_count':0})
    with pytest.raises(AppError):validate_envelope(output,invocation,payload)


async def test_image_must_decode_not_just_have_valid_headers(env):
    output,invocation,payload=await synthetic_output(env)
    buffer=io.BytesIO();Image.new('RGB',(1024,1024),'red').save(buffer,format='JPEG')
    truncated=buffer.getvalue()[:-10]
    output['data'].update(image_base64=base64.b64encode(truncated).decode(),image_bytes=len(truncated),mime_type='image/jpeg')
    payload['output_format']='jpeg'
    with pytest.raises(AppError):validate_envelope(output,invocation,payload)
