"""Offline contract checks; no resolver, downloads, GPU imports, or Docker."""
import contextlib
import copy
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("lock_dependencies", Path(__file__).with_name("lock_dependencies.py"))
lock = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lock)
DIGEST = "a" * 64
IMAGE = "registry.example/gpu:fixed@sha256:" + DIGEST


def inventory():
    return {"schema_version": 1, "python": {"major_minor": "3.12", "version": "3.12.3", "implementation": "CPython"},
            "platform": {"machine": "aarch64", "system": "Linux"},
            "distributions": {"torch": {"version": "2.9.0+vendor", "requires_dist": ["filelock"]},
                              "filelock": {"version": "3.18.0", "requires_dist": []}}}


def wheel(name="demo", version="1.2.3", digest=DIGEST):
    return {"metadata": {"name": name, "version": version, "requires_dist": ["dependency>=2"],
                         "description": "Not retained: lengthy README"},
            "download_info": {"url": f"https://user:password@example.test/{name}-{version}-py3-none-any.whl?token=secret",
                              "archive_info": {"hashes": {"sha256": digest}}}}


def report(*entries):
    return {"version": "1", "pip_version": "25.1", "install": list(entries),
            "environment": {"python_version": "3.12", "platform_machine": "aarch64", "platform_system": "Linux"}}


def generate(data=None):
    return lock.generate(inventory(), data if data is not None else report(wheel()), IMAGE, DIGEST)


class LockTests(unittest.TestCase):
    def test_inventory_uses_metadata_without_importing_gpu_modules(self):
        class Metadata:
            def get(self, key): return "Some_Package"
            def get_all(self, key): return ["Zed>=2", "abc"]
        class Distribution:
            metadata = Metadata()
            version = "1.0+vendor"
            requires = ["Zed>=2", "abc"]
        with patch.object(lock.importlib.metadata, "distributions", return_value=[Distribution()]):
            captured = lock.inventory()
        self.assertEqual(captured["distributions"], {"some-package": {"version": "1.0+vendor", "requires_dist": ["Zed>=2", "abc"]}})
        self.assertNotIn("torch", lock.__dict__)

    def test_inventory_keeps_first_provider_and_all_shadowed_metadata(self):
        class Metadata:
            def __init__(self, name, requires):self.name,self.requires=name,requires
            def get(self, key):return self.name
            def get_all(self, key):return self.requires
        class Distribution:
            def __init__(self,name,version,requires):
                self.metadata,self.version=Metadata(name,requires),version
                self.requires=requires
        entries=[Distribution("six","1.17.0",["active"]), Distribution("six","1.16.0",["os"]),
                 Distribution("Six","1.15.0",["older"]), Distribution("distro","1.9.0",[]),
                 Distribution("distro","1.7.0",[])]
        with patch.object(lock.importlib.metadata,"distributions",return_value=entries):
            captured=lock.inventory()
        self.assertEqual(captured["distributions"]["six"],{"version":"1.17.0","requires_dist":["active"]})
        self.assertEqual(captured["shadowed_distributions"]["six"],
                         [{"version":"1.16.0","requires_dist":["os"]},{"version":"1.15.0","requires_dist":["older"]}])
        self.assertEqual(captured["distributions"]["distro"]["version"],"1.9.0")
        self.assertEqual(captured["shadowed_distributions"]["distro"][0]["version"],"1.7.0")

    def test_shadowed_inventory_is_preserved_and_every_change_detected(self):
        base=inventory();base["shadowed_distributions"]={"filelock":[{"version":"1.0","requires_dist":["legacy>=1"]}]}
        _,provenance=lock.generate(base,report(),IMAGE,DIGEST)
        self.assertEqual(provenance["base_inventory"]["shadowed_distributions"],base["shadowed_distributions"])
        self.assertEqual(lock.verify(provenance,base),[])
        for change in ("version","requirements","addition","removal","copy"):
            current=copy.deepcopy(base)
            if change=="version":current["shadowed_distributions"]["filelock"][0]["version"]="2.0"
            elif change=="requirements":current["shadowed_distributions"]["filelock"][0]["requires_dist"]=["other"]
            elif change=="addition":current["shadowed_distributions"]["torch"]=[{"version":"1.0","requires_dist":[]}]
            elif change=="copy":current["shadowed_distributions"]["filelock"].append(copy.deepcopy(current["shadowed_distributions"]["filelock"][0]))
            else:current["shadowed_distributions"]={}
            with self.subTest(change=change):self.assertTrue(lock.verify(provenance,current))

    def test_old_provenance_without_shadow_field_remains_compatible(self):
        _,provenance=generate(report())
        provenance["base_inventory"].pop("shadowed_distributions")
        self.assertEqual(lock.verify(provenance,inventory()),[])
        current=inventory();current["shadowed_distributions"]={"torch":[{"version":"1.0","requires_dist":[]}]}
        self.assertIn("Added shadowed distributions: torch",lock.verify(provenance,current))

    def test_malformed_shadow_inventory_rejected(self):
        for shadows in ({"unknown":[{"version":"1","requires_dist":[]}]},{"torch":[]},{"torch":[{}]},
                        {"Torch":[{"version":"1"}],"torch":[{"version":"2"}]}):
            data=inventory();data["shadowed_distributions"]=shadows
            with self.subTest(shadows=shadows),self.assertRaises(lock.LockError):lock.validate_inventory(data)

    def test_real_legacy_egg_info_requires_txt_and_shadow_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);legacy=root/"legacy";active=root/"active"
            legacy.mkdir();active.mkdir()
            egg=legacy/"PyGObject-3.42.1.egg-info";egg.mkdir()
            (egg/"PKG-INFO").write_text("Metadata-Version: 1.2\nName: PyGObject\nVersion: 3.42.1\n")
            (egg/"requires.txt").write_text("pycairo>=1.16.3\n")
            legacy_distributions=list(lock.importlib.metadata.distributions(path=[str(legacy)]))
            self.assertEqual(legacy_distributions[0].metadata.get_all("Requires-Dist"),None)
            with patch.object(lock.importlib.metadata,"distributions",return_value=legacy_distributions):
                captured=lock.inventory()
            self.assertEqual(captured["distributions"]["pygobject"]["requires_dist"],["pycairo>=1.16.3"])
            modern=active/"pygobject-3.42.2.dist-info";modern.mkdir()
            (modern/"METADATA").write_text("Metadata-Version: 2.1\nName: PyGObject\nVersion: 3.42.2\nRequires-Dist: pycairo>=1.20\n")
            both=list(lock.importlib.metadata.distributions(path=[str(active),str(legacy)]))
            with patch.object(lock.importlib.metadata,"distributions",return_value=both):
                captured=lock.inventory()
            self.assertEqual(captured["distributions"]["pygobject"],{"version":"3.42.2","requires_dist":["pycairo>=1.20"]})
            self.assertEqual(captured["shadowed_distributions"]["pygobject"],
                             [{"version":"3.42.1","requires_dist":["pycairo>=1.16.3"]}])

    def test_inventory_rejects_duplicate_canonical_names(self):
        data=inventory();data["distributions"]["file_lock"]={"version":"1", "requires_dist":[]}
        data["distributions"]["file-lock"]={"version":"2", "requires_dist":[]}
        with self.assertRaises(lock.LockError):lock.validate_inventory(data)

    def test_sorted_complete_report_lock_and_bounded_provenance(self):
        text, provenance=generate(report(wheel("Zed"),wheel("dependency","2.0")))
        self.assertEqual(text.splitlines()[1:], [f"dependency==2.0 --hash=sha256:{DIGEST}", f"zed==1.2.3 --hash=sha256:{DIGEST}"])
        self.assertEqual(provenance["expected_inventory"], {"dependency":"2.0","filelock":"3.18.0","torch":"2.9.0+vendor","zed":"1.2.3"})
        serialized=json.dumps(provenance)
        for private in ("password","token=", "README", "https://"):
            self.assertNotIn(private,serialized)
        self.assertEqual(provenance["overlay"]["zed"]["requires_dist"],["dependency>=2"])

    def test_empty_overlay_allows_already_satisfied_protected_roots(self):
        text, provenance=generate(report())
        self.assertEqual(len(text.splitlines()),1)
        self.assertEqual(provenance["overlay"],{})
        self.assertEqual(lock.verify(provenance,inventory()),[])

    def test_all_protected_installs_rejected_even_same_version(self):
        for name in ("torch","torchvision","torchaudio","triton","vllm","flashinfer_python","flashinfer-cubin","nvidia-cublas-cu12","nvidia-ml-py","cuda-bindings","transformer-engine","torchao"):
            with self.subTest(name=name),self.assertRaises(lock.LockError):generate(report(wheel(name,"2.9.0+vendor")))
        self.assertIn("pynvml==",generate(report(wheel("pynvml")))[0])

    def test_fresh_base_full_runtime_and_complete_inventory(self):
        base=inventory();base["distributions"].pop("torch")
        text,proof=lock.generate(base,report(wheel("torch","2.9.0"),wheel("nvidia-cublas-cu12"),wheel("triton")),IMAGE,DIGEST,fresh_base=True)
        self.assertEqual(proof["installation_mode"],"fresh-base")
        self.assertIn("torch==2.9.0 --hash=sha256:",text)
        current=copy.deepcopy(base)
        for name,record in proof["overlay"].items():
            current["distributions"][name]={"version":record["version"],"requires_dist":record["requires_dist"]}
        self.assertEqual(lock.verify(proof,current),[])
        current["distributions"].pop("nvidia-cublas-cu12")
        self.assertIn("Missing: nvidia-cublas-cu12==1.2.3",lock.verify(proof,current))
        # Removing the explicit opt-in never permits GPU wheel provenance.
        proof.pop("installation_mode")
        with self.assertRaises(lock.LockError):lock.verify(proof,current)

    def test_fresh_base_cannot_replace_any_preexisting_gpu_stack(self):
        for name in ("torch","triton","nvidia-cublas-cu12","cuda-bindings"):
            base=inventory();base["distributions"].pop("torch")
            base["distributions"][name]={"version":"1.0","requires_dist":[]}
            with self.subTest(name=name),self.assertRaises(lock.LockError):
                lock.generate(base,report(wheel(name,"2.0")),IMAGE,DIGEST,fresh_base=True)
        base=inventory();base["shadowed_distributions"]={"torch":[{"version":"1.0","requires_dist":[]}]}
        with self.assertRaises(lock.LockError):lock.generate(base,report(),IMAGE,DIGEST,fresh_base=True)

    def test_fresh_base_still_rejects_invalid_artifacts_hashes_and_targets(self):
        base=inventory();base["distributions"].pop("torch")
        bad=wheel("torch");bad["download_info"]["archive_info"]={}
        source=wheel("torch");source["download_info"]["url"]="https://example.test/torch.tar.gz"
        wrong=report(wheel("torch"));wrong["environment"]["platform_machine"]="x86_64"
        for data in (report(bad),report(source),wrong):
            with self.subTest(data=data),self.assertRaises(lock.LockError):
                lock.generate(base,data,IMAGE,DIGEST,fresh_base=True)

    def test_verify_rechecks_fresh_base_guard_and_rejects_unknown_mode(self):
        _,proof=generate(report())
        proof["installation_mode"]="fresh-base"
        with self.assertRaises(lock.LockError):lock.verify(proof,inventory())
        proof["installation_mode"]="anything"
        with self.assertRaises(lock.LockError):lock.verify(proof,inventory())

    def test_cli_fresh_base_requires_explicit_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);base=root/"base.json";source=root/"report.json";out=root/"runtime.lock";proof=root/"proof.json"
            clean=inventory();clean["distributions"].pop("torch")
            lock.write_json(base,clean);lock.write_json(source,report(wheel("torch")))
            args=["generate",str(base),str(source),str(out),str(proof),"--base",IMAGE]
            with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(lock.main(args),2)
                self.assertEqual(lock.main(args+["--fresh-base"]),0)
            self.assertEqual(lock.read_json(proof)["installation_mode"],"fresh-base")

    def test_fresh_torch_direct_official_url_keeps_exact_artifact(self):
        base=inventory();base["distributions"].pop("torch")
        for host in ("download.pytorch.org","download-r2.pytorch.org"):
            entry=wheel("torch","2.13.0+cu129");entry["is_direct"]=True
            filename="torch-2.13.0+cu129-cp312-cp312-manylinux_2_28_aarch64.whl"
            url=f"https://{host}/whl/cu129/"+filename.replace("+","%2B")
            entry["download_info"]["url"]=url
            text,proof=lock.generate(base,report(entry),IMAGE,DIGEST,fresh_base=True)
            self.assertEqual(text.splitlines()[1],f"torch @ {url} --hash=sha256:{DIGEST}")
            self.assertEqual(proof["overlay"]["torch"]["artifact_url"],url)
            current=copy.deepcopy(base);current["distributions"]["torch"]={"version":"2.13.0+cu129","requires_dist":[]}
            self.assertEqual(lock.verify(proof,current,lock_text=text),[])
            for bad_text in (text.replace(DIGEST,"b"*64),text.replace(host,"evil.test"),text+"extra==1\n"):
                with self.assertRaises(lock.LockError):lock.verify(proof,current,lock_text=bad_text)
            for field,value in (("artifact_url",url.replace(host,"evil.test")),("sha256","bad"),
                                ("wheel",filename.replace("aarch64","x86_64")),("version","2.12.0")):
                changed=copy.deepcopy(proof);changed["overlay"]["torch"][field]=value
                with self.subTest(field=field),self.assertRaises(lock.LockError):lock.verify(changed,current)

    def test_direct_torch_url_rejects_unsafe_sources_and_wrong_targets(self):
        base=inventory();base["distributions"].pop("torch")
        good="https://download.pytorch.org/whl/cu129/torch-2.13.0%2Bcu129-cp312-cp312-manylinux_2_28_aarch64.whl"
        urls=[good.replace("https:","http:"),good.replace("download.pytorch.org","evil.test"),
              good.replace("download.pytorch.org","download.pytorch.org.evil.test"),
              good.replace("download.pytorch.org","user:secret@download.pytorch.org"),
              good.replace("download.pytorch.org","download.pytorch.org:443"),
              good+"?secret=token",good+"#sha256="+DIGEST,good+"\n",good.replace("/cu129/","/../"),
              good.replace("/cu129/","/cu129/%24/"),good.replace("aarch64","x86_64"),
              good.replace("cp312","cp311"),good.replace("2.13.0","2.12.0"),good.replace("/cu129/","/cu129/;/")]
        for url in urls:
            entry=wheel("torch","2.13.0+cu129");entry["is_direct"]=True;entry["download_info"]["url"]=url
            with self.subTest(url=url),self.assertRaises(lock.LockError):
                lock.generate(base,report(entry),IMAGE,DIGEST,fresh_base=True)

    def test_other_direct_artifact_urls_remain_elided(self):
        base=inventory();base["distributions"].pop("torch")
        entry=wheel("demo");entry["is_direct"]=True
        text,proof=lock.generate(base,report(entry),IMAGE,DIGEST,fresh_base=True)
        self.assertNotIn("artifact_url",proof["overlay"]["demo"])
        self.assertNotIn("https:",text)

    def test_missing_or_invalid_hash_rejected(self):
        for archive in ({},{"hashes":{}},{"hashes":{"sha256":"bad"}},{"hash":"md5=123"}):
            entry=wheel();entry["download_info"]["archive_info"]=archive
            with self.subTest(archive=archive),self.assertRaises(lock.LockError):generate(report(entry))

    def test_legacy_hash_accepted_and_conflict_rejected(self):
        entry=wheel();entry["download_info"]["archive_info"]={"hash":"sha256="+DIGEST}
        self.assertIn(DIGEST,generate(report(entry))[0])
        entry["download_info"]["archive_info"]["hashes"]={"sha256":"b"*64}
        with self.assertRaises(lock.LockError):generate(report(entry))

    def test_source_vcs_and_nonwheel_artifacts_rejected(self):
        for url in ("https://example.test/demo.tar.gz", "git+https://example.test/demo.git", "https://example.test/anything.whl/extra"):
            entry=wheel();entry["download_info"]["url"]=url
            with self.subTest(url=url),self.assertRaises(lock.LockError):generate(report(entry))

    def test_editable_or_vcs_metadata_rejected_even_with_wheel_suffix(self):
        for key in ("dir_info", "vcs_info"):
            entry=wheel();entry["download_info"][key]={}
            with self.subTest(key=key),self.assertRaises(lock.LockError):generate(report(entry))

    def test_duplicate_or_conflicting_install_names_rejected(self):
        for version in ("1.2.3","2.0"):
            with self.assertRaises(lock.LockError):generate(report(wheel("some_pkg"),wheel("some-pkg",version)))

    def test_unpinned_or_malformed_base_rejected(self):
        for image in ("gpu:latest","gpu:fixed","sha256:123", "gpu@sha256:"+"g"*64):
            with self.subTest(image=image),self.assertRaises(lock.LockError):lock.generate(inventory(),report(),image,DIGEST)

    def test_wrong_resolver_target_rejected(self):
        for key,value in (("python_version","3.13"),("platform_machine","x86_64"),("platform_system","Darwin")):
            data=report();data["environment"][key]=value
            with self.subTest(key=key),self.assertRaises(lock.LockError):generate(data)

    def test_verify_complete_inventory_catches_missing_transitive_added_changed(self):
        _,provenance=generate(report(wheel(),wheel("dependency","2.0")))
        current=copy.deepcopy(inventory())
        current["distributions"]["demo"]={"version":"1.2.3","requires_dist":[]}
        current["distributions"]["extra"]={"version":"1","requires_dist":[]}
        current["distributions"]["filelock"]["version"]="0.1"
        differences=lock.verify(provenance,current)
        self.assertIn("Missing: dependency==2.0",differences)
        self.assertIn("Added: extra==1",differences)
        self.assertIn("Changed: filelock 3.18.0 -> 0.1",differences)
        current["distributions"].pop("extra")
        current["distributions"]["dependency"]={"version":"2.0","requires_dist":[]}
        current["distributions"]["filelock"]["version"]="3.18.0"
        self.assertEqual(lock.verify(provenance,current),[])

    def test_verify_architecture_python_and_incomplete_provenance(self):
        _,provenance=generate(report())
        current=inventory();current["platform"]["machine"]="x86_64";current["python"]["major_minor"]="3.11"
        self.assertEqual(len(lock.verify(provenance,current)),2)
        provenance["expected_inventory"].pop("filelock")
        with self.assertRaises(lock.LockError):lock.verify(provenance,inventory())

    def test_duplicate_json_keys_and_unsafe_versions_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"input.json";path.write_text('{"install":[],"install":[]}')
            with self.assertRaises(lock.LockError):lock.read_json(path)
        for version in ("1.*","1.0 --extra-index-url secret","1.0\nsecret",None):
            with self.subTest(version=version),self.assertRaises(lock.LockError):generate(report(wheel(version=version)))

    def test_cli_file_roundtrip_and_safe_failure_output(self):
        with tempfile.TemporaryDirectory() as directory:
            directory=Path(directory);base=directory/"base.json";source=directory/"report.json";out=directory/"overlay.lock";proof=directory/"proof.json"
            lock.write_json(base,inventory());lock.write_json(source,report())
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(lock.main(["generate",str(base),str(source),str(out),str(proof),"--base",IMAGE]),0)
                with patch.object(lock,"inventory",return_value=inventory()):self.assertEqual(lock.main(["verify",str(proof)]),0)
            source.write_text('{"secret":"do-not-log", broken')
            stderr=io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(lock.main(["generate",str(base),str(source),str(out),str(proof),"--base",IMAGE]),2)
            self.assertNotIn("do-not-log",stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
