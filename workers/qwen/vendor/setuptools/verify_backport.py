"""CPU regression proof for the upstream and explicit local backport changes."""
import importlib.metadata
import importlib
import io
import contextlib
import stat
import zipfile
from unittest.mock import patch
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unicodedata
import unittest
from setuptools.command.egg_info import FileList
from setuptools._distutils.filelist import FileList as DistutilsFileList

class BackportTests(unittest.TestCase):
 def test_transparent_local_version(self):
  self.assertEqual(importlib.metadata.version('setuptools'),'80.10.2+pulsefeed.ghsah35f.3')
 def _exclusion(self, directive, candidate):
  for factory in [FileList,DistutilsFileList]:
   for pattern_form,candidate_form in [('NFC','NFD'),('NFD','NFC')]:
    with self.subTest(factory=factory.__module__,directive=directive,pattern=pattern_form,candidate=candidate_form):
     files=factory();files.files=[unicodedata.normalize(candidate_form,candidate),'public.txt']
     files.process_template_line(unicodedata.normalize(pattern_form,directive))
     self.assertEqual(files.files,['public.txt'])
 def test_global_exclude_normalization(self):
  self._exclusion('global-exclude secret_café.txt','secret_café.txt')
 def test_exclude_normalization(self):
  self._exclusion('exclude secret_café.txt','secret_café.txt')
 def test_recursive_exclude_normalization(self):
  self._exclusion('recursive-exclude private_café *.txt','private_café/secret.txt')
 def test_prune_normalization(self):
  self._exclusion('prune private_café','private_café/secret.txt')
 def test_ascii_exclusions_unchanged(self):
  for factory in [FileList,DistutilsFileList]:
   for directive,candidate in [('exclude secret.txt','secret.txt'),('global-exclude secret.txt','secret.txt'),('recursive-exclude private *.txt','private/secret.txt'),('prune private','private/secret.txt')]:
    with self.subTest(factory=factory.__module__,directive=directive):
     files=factory();files.files=[candidate,'public.txt'];files.process_template_line(directive)
     self.assertEqual(files.files,['public.txt'])
 def test_inclusion_preserves_original_filename_form(self):
  for factory in [FileList,DistutilsFileList]:
   with self.subTest(factory=factory.__module__), tempfile.TemporaryDirectory() as folder:
    previous=os.getcwd()
    try:
     os.chdir(folder)
     original=unicodedata.normalize('NFD','public_café.txt')
     Path(original).write_text('public fixture')
     files=factory();files.allfiles=[original];files.process_template_line('global-include public_café.txt')
     self.assertEqual(files.files,[original])
    finally:
     os.chdir(previous)
 def test_explicit_regex_semantics_remain_form_sensitive(self):
  original=unicodedata.normalize('NFD','public_café.txt')
  files=DistutilsFileList();files.files=[original]
  self.assertFalse(files.exclude_pattern('public_café.txt',is_regex=True))
  self.assertEqual(files.files,[original])
 def test_real_sdist_excludes_normalization_collision(self):
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder)
   (root/'setup.py').write_text("from setuptools import setup\nsetup(name='normalization-fixture',version='0.0',py_modules=['fixture'])\n")
   (root/'fixture.py').write_text('VALUE = 1\n')
   (root/'public.txt').write_text('public fixture\n')
   (root/unicodedata.normalize('NFD','secret_café.txt')).write_text('synthetic secret fixture\n')
   (root/'MANIFEST.in').write_text('global-include *.txt\nglobal-exclude secret_café.txt\n')
   result=subprocess.run([sys.executable,'setup.py','sdist','--formats=gztar'],cwd=root,capture_output=True,text=True,timeout=30)
   self.assertEqual(result.returncode,0,result.stdout+result.stderr)
   with tarfile.open(next((root/'dist').glob('*.tar.gz'))) as archive:
    names=[unicodedata.normalize('NFC',name) for name in archive.getnames()]
   self.assertTrue(any(name.endswith('/public.txt') for name in names))
   self.assertFalse(any(name.endswith('/secret_café.txt') for name in names))


 def test_vendored_security_versions(self):
  vendor=Path(importlib.import_module('setuptools').__file__).parent/'_vendor'
  for name,version in [('jaraco.context','6.1.0'),('wheel','0.46.3')]:
   matches=[p for p in vendor.glob('*.dist-info/METADATA') if ('Name: '+name+'\n') in p.read_text()]
   self.assertEqual(len(matches),1)
   self.assertIn('Version: '+version+'\n',matches[0].read_text())
 def _tarball(self, name, *, link=None):
  raw=io.BytesIO()
  with tarfile.open(fileobj=raw,mode='w') as tf:
   member=tarfile.TarInfo(name);member.mode=0o644
   if link is not None:
    member.type=tarfile.SYMTYPE;member.linkname=link;tf.addfile(member)
   else:
    payload=b'benign fixture';member.size=len(payload);tf.addfile(member,io.BytesIO(payload))
  return raw.getvalue()
 def test_vendored_tarball_legitimate_member(self):
  module=importlib.import_module('setuptools._vendor.jaraco.context')
  with tempfile.TemporaryDirectory() as folder:
   target=Path(folder)/'extract'
   with patch.object(module.urllib.request,'urlopen',return_value=io.BytesIO(self._tarball('root/ordinary.txt'))):
    with module.tarball('https://fixture.invalid/benign.tgz',target_dir=target):
     self.assertEqual((target/'ordinary.txt').read_bytes(),b'benign fixture')
   self.assertFalse(target.exists())
 def test_vendored_tarball_rejects_path_and_symlink_escape(self):
  module=importlib.import_module('setuptools._vendor.jaraco.context')
  for name,link in [('root/../outside.txt',None),('root/link','../outside.txt')]:
   with self.subTest(member=name),tempfile.TemporaryDirectory() as folder:
    root=Path(folder);outside=root/'outside.txt';outside.write_text('unchanged');outside.chmod(0o400)
    target=root/'extract'
    with patch.object(module.urllib.request,'urlopen',return_value=io.BytesIO(self._tarball(name,link=link))):
     with self.assertRaises(module.tarfile.FilterError):
      with module.tarball('https://fixture.invalid/benign.tgz',target_dir=target):pass
    self.assertEqual(outside.read_text(),'unchanged');self.assertEqual(stat.S_IMODE(outside.stat().st_mode),0o400)
    self.assertFalse(target.exists())
 def test_vendored_wheel_unpack_preserves_outside_permissions(self):
  WheelFile=importlib.import_module('setuptools._vendor.wheel.wheelfile').WheelFile
  unpack=importlib.import_module('setuptools._vendor.wheel._commands.unpack').unpack
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);dest=root/'dest';dest.mkdir();outside=dest/'sentinel';outside.write_text('unchanged');outside.chmod(0o400)
   wheel=root/'benign-1.0-py3-none-any.whl'
   with WheelFile(wheel,'w') as archive:
    for name,data,mode in [('ordinary.txt',b'ordinary',0o640),('../sentinel',b'benign traversal fixture',0o777),('benign-1.0.dist-info/METADATA',b'Metadata-Version: 2.1\nName: benign\nVersion: 1.0\n',0o644)]:
     entry=zipfile.ZipInfo(name);entry.external_attr=(0o100000|mode)<<16;archive.writestr(entry,data)
   with contextlib.redirect_stdout(io.StringIO()):unpack(str(wheel),str(dest))
   self.assertEqual(outside.read_text(),'unchanged');self.assertEqual(stat.S_IMODE(outside.stat().st_mode),0o400)
   extracted=dest/'benign-1.0'/'ordinary.txt';self.assertEqual(extracted.read_bytes(),b'ordinary');self.assertEqual(stat.S_IMODE(extracted.stat().st_mode),0o640)

if __name__=='__main__':unittest.main(verbosity=2)
