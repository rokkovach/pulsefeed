"""Offline CPU regressions for the selected Runpod1.10 security backport.

Uses an explicit wheel on sys.path, without installing or contacting any service.
Run with a Python environment containing Runpod's ordinary CPU dependencies.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import csv
from email.parser import BytesParser
import hashlib
import importlib.metadata
import io
import json
import multiprocessing
import os
from pathlib import Path
import pickle
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parent
PROVENANCE = json.loads((ROOT / 'provenance.json').read_text())
WHEEL = BASE = REFERENCE = None


def mirror_child(mirror, sender):
    sender.send(mirror.get())
    sender.close()


class Response:
    def __init__(self,status=200,headers=None,chunks=()):
        self.status_code=status;self.headers=headers or {};self.chunks=chunks;self.closed=False
    def close(self):self.closed=True
    def raise_for_status(self):
        if self.status_code>=400:
            from requests import HTTPError
            raise HTTPError('synthetic HTTP rejection')
    def iter_content(self,chunk_size):yield from self.chunks
    def __enter__(self):return self
    def __exit__(self,*args):self.close()


class BackportTests(unittest.TestCase):
    def setUp(self):
        self.env = {key:os.environ.pop(key) for key in list(os.environ) if key.startswith('RUNPOD_')}
        self.network = mock.patch('socket.socket.connect',side_effect=AssertionError('Network forbidden in backport tests'))
        self.network.start()
        self.proxy = mock.patch.object(ssrf,'get_environ_proxies',return_value={})
        self.proxy.start()
    def tearDown(self):
        self.proxy.stop();self.network.stop()
        for key in list(os.environ):
            if key.startswith('RUNPOD_'):os.environ.pop(key)
        os.environ.update(self.env)

    def test_local_version_and_unchanged_dependency_declarations(self):
        self.assertEqual(runpod.__version__,PROVENANCE['local_version'])
        self.assertEqual(importlib.metadata.version('runpod'),PROVENANCE['local_version'])
        with zipfile.ZipFile(WHEEL) as candidate,zipfile.ZipFile(BASE) as base:
            metadata=BytesParser().parsebytes(candidate.read(f'runpod-{PROVENANCE["local_version"]}.dist-info/METADATA'))
            upstream=BytesParser().parsebytes(base.read('runpod-1.10.0.dist-info/METADATA'))
            self.assertEqual(metadata.get_all('Requires-Dist'),upstream.get_all('Requires-Dist'))
            self.assertEqual(metadata['Requires-Python'],upstream['Requires-Python'])
            self.assertIn('fastapi[all]>=0.94.0',metadata.get_all('Requires-Dist'))
            self.assertEqual(metadata['Version'],PROVENANCE['local_version'])

    def test_record_hashes_sizes_complete_paths_and_license(self):
        with zipfile.ZipFile(WHEEL) as wheel:
            info=f'runpod-{PROVENANCE["local_version"]}.dist-info'
            records=list(csv.reader(io.StringIO(wheel.read(info+'/RECORD').decode())))
            self.assertEqual({row[0] for row in records},set(wheel.namelist()))
            self.assertEqual(len(records),len(wheel.namelist()))
            for name,digest,size in records:
                if name.endswith('/RECORD'):
                    self.assertEqual((digest,size),('',''));continue
                raw=wheel.read(name)
                self.assertEqual(digest,'sha256='+base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode().rstrip('='))
                self.assertEqual(int(size),len(raw))
            self.assertEqual(wheel.read(info+'/licenses/LICENSE'),(ROOT/'LICENSE').read_bytes())
            embedded=json.loads(wheel.read(info+'/PULSEFEED-BACKPORT.json'))
            self.assertEqual(embedded['patches'],PROVENANCE['patches'])

    def test_only_reviewed_sdk_files_differ_from_base_and_match_reference(self):
        changed={item['path'] for patch in PROVENANCE['patches'] for item in patch['files']}
        with zipfile.ZipFile(WHEEL) as candidate,zipfile.ZipFile(BASE) as base,zipfile.ZipFile(REFERENCE) as reference:
            for name in base.namelist():
                if name.startswith('runpod/'):
                    self.assertEqual(candidate.read(name),reference.read(name) if name in changed else base.read(name),name)
            self.assertEqual({name for name in candidate.namelist() if name.startswith('runpod/')} - set(base.namelist()),{'runpod/serverless/utils/rp_ssrf.py'})
            self.assertEqual(candidate.read('runpod/serverless/modules/rp_job.py'),base.read('runpod/serverless/modules/rp_job.py'))

    def test_rebuild_is_byte_identical_and_rejects_bad_sources_and_patches(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('backport_builder',ROOT/'build_backport.py')
        builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)
        raw=builder.assemble(BASE,PROVENANCE,ROOT)
        self.assertEqual(raw,WHEEL.read_bytes())
        with tempfile.TemporaryDirectory() as folder:
            bad=Path(folder)/'bad.whl';bad.write_bytes(b'not upstream')
            with self.assertRaises(ValueError):builder.assemble(bad,PROVENANCE,ROOT)
        bad=json.loads(json.dumps(PROVENANCE));bad['patches'][0]['sha256']='0'*64
        with self.assertRaises(ValueError):builder.assemble(BASE,bad,ROOT)

    def test_malicious_pickle_is_never_loaded_and_tracker_never_touches_state_file(self):
        class Poison:
            def __reduce__(self):return os.mkdir,(str(marker),)
        previous=state.JobsProgress._instance
        with tempfile.TemporaryDirectory() as folder:
            marker=Path(folder)/'would-execute';path=Path(folder)/'.runpod_jobs.pkl'
            raw=pickle.dumps({Poison()});path.write_bytes(raw)
            old=os.getcwd()
            try:
                os.chdir(folder);state.JobsProgress._instance=None
                original_open=open
                def deny_state(file,*args,**kwargs):
                    if '.runpod_jobs' in str(file):raise AssertionError('Job-state filesystem access forbidden')
                    return original_open(file,*args,**kwargs)
                with mock.patch('builtins.open',side_effect=deny_state):
                    jobs=state.JobsProgress();jobs.add('new-job');jobs.remove('new-job');jobs.clear()
                    self.assertIsNone(jobs.get_job_list())
                self.assertFalse(marker.exists());self.assertEqual(path.read_bytes(),raw)
                self.assertFalse((Path(folder)/'.runpod_jobs.pkl.lock').exists())
            finally:state.JobsProgress._instance=previous;os.chdir(old)

    def test_job_mirror_tracks_add_remove_clear_and_truncation_without_files(self):
        previous=state.JobsProgress._instance
        try:
            state.JobsProgress._instance=None
            jobs=state.JobsProgress();mirror=state.PingJobMirror()
            jobs.set_mirror(mirror);jobs.add('one');jobs.add({'id':'two'})
            self.assertEqual(set(mirror.get().split(',')),{'one','two'})
            jobs.remove('one');self.assertEqual(mirror.get(),'two')
            jobs.clear();self.assertIsNone(mirror.get())
            short=state.PingJobMirror(capacity=8);short.set('one,two,three');self.assertEqual(short.get(),'one')
        finally:state.JobsProgress._instance=previous

    def test_spawned_ping_process_reads_shared_mirror(self):
        ctx=multiprocessing.get_context('spawn');mirror=state.PingJobMirror(ctx=ctx)
        mirror.set('job-a,job-b');receiver,sender=ctx.Pipe(duplex=False)
        child=ctx.Process(target=mirror_child,args=(mirror,sender))
        try:
            child.start();sender.close()
            self.assertTrue(receiver.poll(10),'Spawned process did not receive shared state')
            self.assertEqual(receiver.recv(),'job-a,job-b')
            child.join(10);self.assertEqual(child.exitcode,0)
        finally:
            if child.is_alive():child.terminate();child.join(5)
            receiver.close()

    def test_heartbeat_uses_injected_mirror_not_local_tracker(self):
        heartbeat=ping.Heartbeat();mirror=mock.Mock();mirror.get.return_value='parent-job'
        heartbeat._mirror=mirror;heartbeat._session=mock.Mock()
        heartbeat._session.get.return_value=SimpleNamespace(url='https://host.invalid/ping',status_code=200)
        heartbeat._send_ping()
        self.assertEqual(heartbeat._session.get.call_args.kwargs['params']['job_id'],'parent-job')
        self.assertEqual(heartbeat._session.get.call_args.kwargs['params']['runpod_version'],PROVENANCE['local_version'])
        heartbeat._mirror=None;heartbeat._send_ping()
        self.assertIsNone(heartbeat._session.get.call_args.kwargs['params']['job_id'])

    def test_fitness_report_failure_still_forces_exit(self):
        def fail():raise RuntimeError('synthetic unfit worker')
        with mock.patch.object(fitness,'_fitness_checks',[fail]),mock.patch.object(fitness,'_ensure_gpu_check_registered'),mock.patch.object(fitness,'_ensure_system_checks_registered'),mock.patch.object(fitness,'_report_unhealthy',side_effect=RuntimeError('synthetic report failure')),mock.patch.object(fitness,'_terminate_unhealthy',side_effect=SystemExit(7)) as exit_mock:
            with self.assertRaises(SystemExit):asyncio.run(fitness.run_fitness_checks())
            exit_mock.assert_called_once_with(1)

    def test_fitness_report_is_bounded_and_closes_session_after_failure(self):
        os.environ.update(RUNPOD_WEBHOOK_PING='https://host.invalid/$RUNPOD_POD_ID',RUNPOD_AI_API_KEY='synthetic-test-only')
        client=mock.Mock();client.get.side_effect=RuntimeError('synthetic network failure')
        with mock.patch('runpod.http_client.SyncClientSession',return_value=client):fitness._report_unhealthy('test_check','reason')
        self.assertEqual(client.get.call_args.kwargs['timeout'],2)
        client.close.assert_called_once()

    def test_hard_exit_does_not_wait_for_non_daemon_threads(self):
        code="import sys,threading,os;sys.path.insert(0,sys.argv[1]);from unittest import mock;exists=os.path.exists\nwith mock.patch('os.path.exists',side_effect=lambda p:False if '.runpod' in str(p) else exists(p)):\n from runpod.serverless.modules.rp_fitness import _terminate_unhealthy\nthreading.Thread(target=lambda:threading.Event().wait(60),daemon=False).start();_terminate_unhealthy(7)"
        result=subprocess.run([sys.executable,'-c',code,str(WHEEL)],timeout=5,capture_output=True)
        self.assertEqual(result.returncode,7)

    def test_ssrf_denies_nonpublic_ipv4_ipv6_mapped_metadata_and_cgnat(self):
        for address in ['127.0.0.1','10.1.2.3','172.16.1.1','192.168.1.1','169.254.169.254','100.64.0.1','224.0.0.1','0.0.0.0','::1','fc00::1','fe80::1','::ffff:10.1.2.3','not-an-ip']:
            with self.subTest(address=address):self.assertFalse(ssrf.is_safe_address(address))
        for address in ['8.8.8.8','1.1.1.1','2606:4700:4700::1111']:self.assertTrue(ssrf.is_safe_address(address))

    def test_ssrf_blocks_mixed_dns_answers_and_bad_schemes(self):
        def answer(ip):return (socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,443))
        with mock.patch.object(ssrf.socket,'getaddrinfo',return_value=[answer('8.8.8.8'),answer('127.0.0.1')]):
            with self.assertRaises(ssrf.SSRFError):ssrf.resolve_and_validate('mixed.invalid',443)
        with mock.patch.object(ssrf.socket,'getaddrinfo',side_effect=AssertionError('Scheme must fail before DNS')):
            with self.assertRaises(ssrf.SSRFError):ssrf.safe_get('file:///etc/passwd')

    def test_ssrf_pinning_preserves_hostname_verification_sni_and_host_header(self):
        pool=SimpleNamespace(host='origin.invalid',conn_kw={'unchanged':True})
        adapter=ssrf.PinnedIPAdapter('8.8.8.8')
        self.assertIs(adapter._pin(pool,'https://origin.invalid/file'),pool)
        self.assertEqual(pool.host,'8.8.8.8');self.assertEqual(pool.assert_hostname,'origin.invalid')
        self.assertEqual(pool.conn_kw,{'unchanged':True,'server_hostname':'origin.invalid'})
        self.assertEqual(ssrf._host_header_for('https://[2606:4700:4700::1111]:8443/file'),'[2606:4700:4700::1111]:8443')
        adapter.close()

    def test_ssrf_redirect_revalidates_target_and_closes_session(self):
        response=Response(302,{'Location':'http://127.0.0.1/private'});client=mock.Mock()
        with mock.patch.object(ssrf,'resolve_and_validate',side_effect=[['8.8.8.8'],ssrf.SSRFError('blocked redirect')]) as resolve,mock.patch.object(ssrf,'_get_via_first_reachable_ip',return_value=(client,response)) as get:
            with self.assertRaises(ssrf.SSRFError):ssrf.safe_get('https://public.invalid/file')
            self.assertEqual(resolve.call_count,2);get.assert_called_once()
        self.assertTrue(response.closed);client.close.assert_called_once()

    def test_ssrf_proxy_routes_fail_closed(self):
        with mock.patch.object(ssrf,'get_environ_proxies',return_value={'https':'http://proxy.invalid:8080'}),mock.patch.object(ssrf,'resolve_and_validate',side_effect=AssertionError('Proxy must be rejected first')):
            with self.assertRaises(ssrf.SSRFError):ssrf.safe_get('https://public.invalid/file')

    def test_ssrf_declared_and_streamed_size_caps_close_response(self):
        client=mock.Mock();response=Response(headers={'Content-Length':'11'})
        with mock.patch.object(ssrf,'resolve_and_validate',return_value=['8.8.8.8']),mock.patch.object(ssrf,'_get_via_first_reachable_ip',return_value=(client,response)):
            with self.assertRaises(ssrf.SSRFError):ssrf.safe_get('https://public.invalid/file',max_bytes=10)
        self.assertTrue(response.closed);client.close.assert_called_once()
        streamed=Response(chunks=[b'123',b'',b'456'])
        with self.assertRaises(ssrf.SSRFError):list(ssrf.iter_content_capped(streamed,2,5))
        self.assertTrue(streamed.closed)

    def test_ssrf_dual_stack_connection_fallback_and_successful_cleanup(self):
        first,second=mock.Mock(),mock.Mock();first.get.side_effect=ssrf.RequestsConnectionError('synthetic no IPv6 route')
        response=Response(chunks=[b'ok']);second.get.return_value=response
        with mock.patch.object(ssrf,'_build_pinned_session',side_effect=[first,second]):
            client,got=ssrf._get_via_first_reachable_ip(['2606:4700:4700::1111','8.8.8.8'],'https://public.invalid/file',headers={},stream=True,timeout=3)
        self.assertIs(client,second);self.assertIs(got,response);first.close.assert_called_once()
        self.assertFalse(second.get.call_args.kwargs['allow_redirects'])
        ssrf._bind_session_to_response(response,second);response.close();second.close.assert_called_once()

    def test_download_entrypoints_enforce_stream_cap_and_do_not_retry_ssrf(self):
        os.environ['RUNPOD_MAX_DOWNLOAD_BYTES']='3'
        old=os.getcwd()
        with tempfile.TemporaryDirectory() as folder:
            try:
                os.chdir(folder)
                for callback in [download.file,lambda url:download.download_files_from_urls('synthetic-job',[url])]:
                    response=Response(headers={'Content-Length':'invalid'},chunks=[b'ab',b'cd'])
                    with mock.patch.object(download,'safe_get',return_value=response) as get:
                        with self.assertRaises(ssrf.SSRFError):callback('https://public.invalid/file.txt')
                        get.assert_called_once()
                    self.assertTrue(response.closed)
            finally:os.chdir(old)

    def test_actual_qwen_handler_envelopes_survive_unchanged_sdk_run_job(self):
        from workers.qwen import handler as qwen
        class FakeRuntime:
            ready=True
            metadata={'id':qwen.MODEL_ID,'revision':'synthetic-test-only','precision':'NVFP4','runtime':'synthetic-test-only'}
            def prepare(self,*args):return [1,2]
            def generate(self,*args):return qwen.Result('Editorial watercolor of people planting an urban garden, balanced light.','stop',12,5,1)
        value={'schema_version':1,'stage':'qwen','run_id':'11111111-1111-4111-8111-111111111111','invocation_id':'22222222-2222-4222-8222-222222222222','deadline_unix_ms':200000,'article':{'title':'Gardens','excerpt':'Neighbors plant trees.'},'system_prompt':'Create a visual prompt.','max_new_tokens':256}
        worker=qwen.QwenWorker(FakeRuntime(),clock=lambda:100,monotonic=lambda:10)
        with mock.patch.object(qwen,'WORKER',worker):
            for input_value,expected in [(value,True),({},False)]:
                response=asyncio.run(job.run_job(qwen.handler,{'id':'synthetic-sdk-job','input':input_value}))
                self.assertEqual(set(response),{'output'})
                self.assertEqual(set(response['output']),{'result'})
                self.assertEqual(response['output']['result']['ok'],expected)
                self.assertIn('error',response['output']['result'])
        self.assertTrue(callable(runpod.serverless.start))


def main():
    global WHEEL,BASE,REFERENCE,runpod,state,ping,fitness,ssrf,download,job
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel',type=Path,default=ROOT/PROVENANCE['candidate_wheel']['filename'])
    parser.add_argument('--base-wheel',type=Path,required=True)
    parser.add_argument('--reference-wheel',type=Path,required=True)
    args=parser.parse_args();WHEEL,BASE,REFERENCE=(value.resolve() for value in (args.wheel,args.base_wheel,args.reference_wheel))
    for path,key in [(WHEEL,'candidate_wheel'),(BASE,'base_wheel'),(REFERENCE,'reference_wheel')]:
        if hashlib.sha256(path.read_bytes()).hexdigest()!=PROVENANCE[key]['sha256']:raise ValueError('Wheel hash mismatch')
    for key in list(os.environ):
        if key.startswith('RUNPOD_'):os.environ.pop(key)
    sys.path.insert(0,str(WHEEL))
    # Runpod's public root imports its CLI credential loader. Suppress only that
    # optional local lookup; never read an operator's credential file in tests.
    exists=os.path.exists
    with mock.patch('os.path.exists',side_effect=lambda path:False if '.runpod' in str(path) else exists(path)):
        import runpod
        from runpod.serverless.modules import worker_state as state,rp_ping as ping,rp_fitness as fitness,rp_job as job
        from runpod.serverless.utils import rp_ssrf as ssrf,rp_download as download
    # Repository imports are only for the actual CPU-injected Qwen contract test.
    sys.path.insert(1,str(ROOT.parents[3]))
    unittest.main(argv=[sys.argv[0]],verbosity=2)


if __name__=='__main__':main()
