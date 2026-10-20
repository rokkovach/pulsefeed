"""Exercise release orchestration with fake commands, never Docker or downloads."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'scripts' / 'runpod-images.sh'


class RunpodImagesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='release orchestration ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'checkout with spaces'
        (self.repo / 'scripts').mkdir(parents=True)
        (self.repo / 'workers' / 'qwen').mkdir(parents=True)
        (self.repo / 'workers' / 'flux').mkdir(parents=True)
        self.script = self.repo / 'scripts' / 'runpod-images.sh'
        shutil.copy2(SOURCE, self.script)
        self.bin = self.root / 'bin'; self.bin.mkdir()
        self.tmp = self.root / 'private temporary'; self.tmp.mkdir()
        self.log = self.root / 'calls.jsonl'
        fake = '''#!INTERPRETER
import json,os,pathlib,sys
name=pathlib.Path(sys.argv[0]).name
args=sys.argv[1:]
with open(os.environ['RELEASE_TEST_LOG'],'a') as stream:stream.write(json.dumps([name,args])+'\\n')
if name=='mktemp':raise SystemExit(99)
if name=='python3':
    out=pathlib.Path(args[args.index('--output-dir')+1])
    assert not out.exists() and out.parent.stat().st_mode & 0o777 == 0o700
    assert pathlib.Path(os.environ['TMPDIR']) in out.parents
    out.mkdir();(out/'prepared').write_text('offline fixture')
    stage='prepare'
else:
    stage='qwen' if 'qwen-' in ' '.join(args) or '/pulsefeed-qwen:' in ' '.join(args) else 'flux'
    if stage=='qwen':assert (pathlib.Path(args[-1])/'prepared').is_file()
if os.environ.get('RELEASE_TEST_FAIL')==stage:raise SystemExit(7)
'''.replace('INTERPRETER', sys.executable)
        for name in ('python3', 'docker'):
            path = self.bin / name;path.write_text(fake);path.chmod(0o755)
        self.fake = fake
        self.env = dict(os.environ, PATH=str(self.bin)+os.pathsep+os.environ['PATH'],
                        TMPDIR=str(self.tmp), RELEASE_TEST_LOG=str(self.log))
        self.args = ['--registry', 'docker.io/example', '--version', 'offline-20261020']

    def run_script(self, *extra, env=None):
        return subprocess.run(['bash', str(self.script), *self.args, *extra], env=env or self.env,
                              capture_output=True, text=True)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_default_plan_is_side_effect_free_and_executable(self):
        fake_mktemp=self.bin/'mktemp';fake_mktemp.write_text(self.fake);fake_mktemp.chmod(0o755)
        result=self.run_script()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.calls(),[])
        self.assertEqual(list(self.tmp.iterdir()),[])
        self.assertIn('--platform amd64 --output-dir "$build_root/qwen"',result.stdout)
        self.assertIn("trap 'rm -rf --",result.stdout)
        self.assertEqual(subprocess.run(['bash','-n'],input=result.stdout,text=True,capture_output=True).returncode,0)
        fake_mktemp.unlink()
        executed=subprocess.run(['bash'],input=result.stdout,text=True,env=self.env,capture_output=True)
        self.assertEqual(executed.returncode,0,executed.stderr)
        self.assertEqual([call[0] for call in self.calls()],['python3','docker','docker'])
        self.assertEqual(list(self.tmp.iterdir()),[])

    def test_build_and_push_prepare_private_context_then_remove_it(self):
        for mode in ('--build','--push'):
            with self.subTest(mode=mode):
                self.log.unlink(missing_ok=True)
                result=self.run_script('--repository','separate-models',mode)
                self.assertEqual(result.returncode,0,result.stderr)
                calls=self.calls();self.assertEqual([x[0] for x in calls],['python3','docker','docker'])
                prepared=calls[0][1][-1]
                self.assertEqual(calls[1][1][-1],prepared)
                self.assertIn('--push' if mode=='--push' else '--load',calls[1][1])
                self.assertIn('--push' if mode=='--push' else '--load',calls[2][1])
                self.assertNotEqual(calls[1][1][-1],calls[2][1][-1])
                self.assertEqual(list(self.tmp.iterdir()),[])

    def test_cleanup_on_preparation_or_either_build_failure(self):
        for stage,expected in (('prepare',1),('qwen',2),('flux',3)):
            with self.subTest(stage=stage):
                self.log.unlink(missing_ok=True)
                env=dict(self.env,RELEASE_TEST_FAIL=stage)
                result=self.run_script('--build',env=env)
                self.assertEqual(result.returncode,7,result.stderr)
                self.assertEqual(len(self.calls()),expected)
                self.assertEqual(list(self.tmp.iterdir()),[])

    def test_invalid_registry_is_rejected_before_commands(self):
        result=subprocess.run(['bash',str(self.script),'--registry','https://evil.test/team',
                               '--version','offline-only','--push'],env=self.env,capture_output=True,text=True)
        self.assertEqual(result.returncode,2)
        self.assertEqual(self.calls(),[])
        self.assertEqual(list(self.tmp.iterdir()),[])


if __name__ == '__main__':
    unittest.main()
