from pathlib import Path
import os,sys,pytest
pytestmark=pytest.mark.skipif(os.name != "posix", reason="POSIX adapter only")
SCRIPTS=Path(__file__).resolve().parents[1]/"skills/token-optimizer/scripts"
sys.path.insert(0,str(SCRIPTS))

def _fixture_manifest(root):
    import devin_source,hashlib,json
    # Direct loader tests still use a complete committed source identity set.
    for name in devin_source.PAYLOAD:
        source=root/name
        if not source.exists():source.write_text('VALUE=42')
        source.chmod(0o600)
    manifest=root.parent/'install.json'
    manifest.write_text(json.dumps({'owner':'devin-output-filter-v1','files':{
        str(root/name):hashlib.sha256((root/name).read_bytes()).hexdigest() for name in devin_source.PAYLOAD}}))
    manifest.chmod(0o600)


def test_unmanifested_source_module_is_not_loadable(tmp_path):
    import devin_source
    (tmp_path/'unmanifested_test.py').write_text('VALUE="not executed"')
    assert devin_source.SourceOnly(tmp_path).find_spec('unmanifested_test') is None


def test_unmanifested_source_file_refuses_integrity(tmp_path, monkeypatch):
    import devin_install as d
    from devin_verify import verify_install
    monkeypatch.setenv('HOME',str(tmp_path));d.install()
    (d.state.data_dir()/'plugin/unmanifested_test.py').write_text('VALUE="not executed"')
    with pytest.raises(OSError,match='unmanifested'):verify_install()

@pytest.mark.parametrize('kind',['package','namespace','pyc','extension','pth','case_variant'])
def test_nonmanifested_artifacts_refused(tmp_path, monkeypatch, kind):
    import devin_install as d
    from devin_verify import verify_install
    monkeypatch.setenv('HOME',str(tmp_path));d.install();root=d.state.data_dir()/'plugin'
    if kind in ('package','namespace'):
        extra=root/'unmanifested';extra.mkdir()
        if kind=='package':(extra/'__init__.py').write_text('VALUE=1')
    else:
        # Exact entry-name policy is independent of extension/case sensitivity.
        name={'pyc':'extra.pyc','extension':'extra.so','pth':'extra.pth','case_variant':'extra.PY'}[kind]
        (root/name).write_bytes(b'not executed')
    with pytest.raises(OSError,match='unmanifested'):verify_install()


def test_import_gate_manifest_have_one_set():
    import devin_source,devin_verify
    assert devin_source.PAYLOAD == devin_verify.PAYLOAD
    assert set(devin_source.ALLOWED_MODULES)=={Path(name).stem for name in devin_verify.PAYLOAD}


def test_runtime_import_confinement_and_positive_control(tmp_path):
    import subprocess
    # Activation must remove the installed payload from path-based import routes.
    (tmp_path/'extra.py').write_text('raise RuntimeError("must not execute")')
    (tmp_path/'token_estimate.py').write_text('VALUE=42');(tmp_path/'token_estimate.py').chmod(0o600)
    _fixture_manifest(tmp_path)
    code=('import sys;sys.path.insert(0,'+repr(str(SCRIPTS))+');import devin_source;'
          'from pathlib import Path;root=Path('+repr(str(tmp_path))+');'
          'sys.path.insert(0,str(root));devin_source.activate(root);'
          'import token_estimate;assert token_estimate.VALUE==42;'
          'assert isinstance(token_estimate.__loader__,devin_source.SourceOnly);'
          'import importlib.util;assert importlib.util.find_spec("extra") is None')
    subprocess.run([sys.executable,'-I','-c',code],check=True,timeout=10)


def test_missing_allowed_source_fails_closed(tmp_path):
    import devin_source
    with pytest.raises(ImportError):devin_source.SourceOnly(tmp_path).find_spec('token_estimate')

@pytest.mark.parametrize('route',['ancestor','preloaded','empty','alias'])
def test_activation_runtime_routes_closed(tmp_path, route):
    import subprocess
    root=tmp_path/'payload';root.mkdir();source=root/'token_estimate.py';source.write_text('VALUE=42');source.chmod(0o600)
    (root/'unmanifested.py').write_text('VALUE="forbidden"')
    _fixture_manifest(root)
    code='''import sys,os,types,importlib.util
sys.path.insert(0,SCRIPTS)
import devin_source
root=ROOT
if ROUTE=='ancestor':sys.path.insert(0,str(root.parent))
if ROUTE=='preloaded':
    fake=types.ModuleType('token_estimate');fake.VALUE=0;sys.modules['token_estimate']=fake
if ROUTE=='empty':os.chdir(root);sys.path.insert(0,'')
if ROUTE=='alias':
    alias=root.parent/'alias';alias.symlink_to(root,target_is_directory=True);sys.path.insert(0,str(alias))
devin_source.activate(root)
import token_estimate
assert token_estimate.VALUE==42 and isinstance(token_estimate.__loader__,devin_source.SourceOnly)
assert importlib.util.find_spec('unmanifested') is None
assert importlib.util.find_spec('payload') is None
'''
    code='from pathlib import Path\nSCRIPTS='+repr(str(SCRIPTS))+'\nROOT=Path('+repr(str(root))+')\nROUTE='+repr(route)+'\n'+code
    subprocess.run([sys.executable,'-c',code],check=True,timeout=10)

@pytest.mark.parametrize('kind',['directory_symlink','nested','non_pyc','hardlink'])
def test_cache_exception_refuses_unsafe_artifacts(tmp_path, monkeypatch, kind):
    import devin_install as d
    from devin_verify import verify_install
    monkeypatch.setenv('HOME',str(tmp_path));d.install();root=d.state.data_dir()/'plugin';cache=root/'__pycache__'
    if kind=='directory_symlink':
        target=tmp_path/'cache';target.mkdir();cache.symlink_to(target,target_is_directory=True)
    else:
        cache.mkdir()
        if kind=='nested':(cache/'nested').mkdir()
        elif kind=='non_pyc':(cache/'note.txt').write_text('foreign')
        else:
            target=tmp_path/'x.pyc';target.write_bytes(b'cache');os.link(target,cache/'x.pyc')
    with pytest.raises(OSError):verify_install()


def test_clean_incidental_cache_and_clean_install_verify(tmp_path, monkeypatch):
    import devin_install as d
    from devin_verify import verify_install
    monkeypatch.setenv('HOME',str(tmp_path));d.install()
    assert verify_install()
    cache=d.state.data_dir()/'plugin/__pycache__';cache.mkdir()
    (cache/'token_estimate.cpython-312.pyc').write_bytes(b'ignored cache')
    assert verify_install()

def test_in_flight_bytecode_write_is_not_an_artifact(tmp_path, monkeypatch):
    """Parallel first runs see importlib's NAME.pyc.<id> before its rename."""
    import devin_install as d
    from devin_verify import verify_install
    monkeypatch.setenv('HOME',str(tmp_path));d.install()
    cache=d.state.data_dir()/'plugin/__pycache__';cache.mkdir()
    (cache/'devin_hooks.cpython-311.pyc.140212345678').write_bytes(b'in flight')
    assert verify_install()
    (cache/'devin_hooks.cpython-311.pyc.tmp').write_bytes(b'foreign')
    with pytest.raises(OSError):verify_install()

@pytest.mark.parametrize('kind',['symlink','hardlink','mode','owner','oversize'])
def test_allowed_source_open_checks(tmp_path, monkeypatch, kind):
    import devin_source,importlib.util,types,stat
    source=tmp_path/'token_estimate.py';source.write_text('VALUE=42');source.chmod(0o600)
    if kind=='symlink':
        target=tmp_path/'target';source.rename(target);source.symlink_to(target)
    elif kind=='hardlink':os.link(source,tmp_path/'linked')
    elif kind=='mode':source.chmod(0o644)
    elif kind=='oversize':
        with source.open('ab') as out:out.truncate(8*1024*1024+1)
    loader=devin_source.SourceOnly(tmp_path)
    # The foreign-owner fstat stands in only while the loader runs: left in
    # place, pytest's own output capture calls os.fstat(...).st_size after the
    # test body (Linux) and fails on the stand-in.
    with monkeypatch.context() as patch:
        if kind=='owner':
            original=os.fstat
            def foreign(fd):
                result=original(fd)
                return types.SimpleNamespace(st_mode=result.st_mode,st_uid=result.st_uid+1,st_nlink=result.st_nlink)
            patch.setattr(os,'fstat',foreign)
        # Each case must trip its own check, not the empty-manifest digest
        # mismatch every unmanifested source would hit anyway.
        reason={'symlink':'regular payload source required','oversize':'payload source exceeds bound'}.get(
            kind,'private regular payload source required')
        with pytest.raises((OSError,ImportError),match=reason):
            spec=loader.find_spec('token_estimate');module=importlib.util.module_from_spec(spec);loader.exec_module(module)


def test_real_installed_chain_and_stdlib_after_activation(tmp_path, monkeypatch):
    import devin_install as d,subprocess,json
    monkeypatch.setenv('HOME',str(tmp_path));d.install();root=d.state.data_dir()/'plugin'
    code='''import sys,os,types
from pathlib import Path
root=Path(sys.argv[1])
namespace={}
exec(compile((root/'devin_source.py').read_bytes(),str(root/'devin_source.py'),'exec'),namespace)
sys.path.insert(0,str(root.parent));sys.path.insert(0,str(root));os.chdir(root);sys.path.insert(0,'')
for name in namespace['ALLOWED_MODULES']:
    fake=types.ModuleType(name);fake.FAKE=True;sys.modules[name]=fake
namespace['activate'](root)
import devin_source,devin_state,devin_verify,devin_compress,devin_doctor
import bash_compress,credential_patterns,token_estimate,command_filters,runtime_env,py_trust
import email.parser,decimal,fractions
assert devin_verify.verify_install()
for name in namespace['ALLOWED_MODULES']:
    __import__(name)
    module=sys.modules[name]
    assert not getattr(module,'FAKE',False)
    assert module.__loader__.__class__.__name__=='SourceOnly'
print('installed source chain and stdlib imports passed')
'''
    run=subprocess.run([sys.executable,'-c',code,str(root)],env=dict(os.environ),capture_output=True,timeout=15)
    assert run.returncode==0,run.stderr.decode()
    run=subprocess.run([sys.executable,str(root/'devin_doctor.py'),'--probe'],env=dict(os.environ),capture_output=True,timeout=20)
    assert run.returncode==0,run.stderr.decode()
    assert json.loads(run.stdout)['local_probe_passed']


def test_cache_world_writable_refused(tmp_path,monkeypatch):
    import devin_install as d
    from devin_verify import verify_install
    monkeypatch.setenv('HOME',str(tmp_path));d.install();cache=d.state.data_dir()/'plugin/__pycache__';cache.mkdir();cache.chmod(0o777)
    with pytest.raises(OSError):verify_install()


def test_distribution_mirrors_match_canonical_source():
    repo=SCRIPTS.parents[2]
    for name in ('devin_source.py','devin_verify.py','devin_state.py','devin_install.py','devin_doctor.py','devin_compress.py','bash_compress.py'):
        for tree in ('plugins','cowork'):
            assert (SCRIPTS/name).read_bytes()==(repo/tree/'token-optimizer/skills/token-optimizer/scripts'/name).read_bytes()


def test_real_bootstrap_ignores_forged_cache_for_source_and_state(tmp_path, monkeypatch):
    import devin_install as d,py_compile,subprocess
    monkeypatch.setenv('HOME',str(tmp_path));d.install();root=d.state.data_dir()/'plugin'
    marker=tmp_path/'FORGED_CACHE_EXECUTED'
    for name in ('devin_source.py','devin_state.py'):
        target=root/name;original=target.read_bytes();mtime=target.stat().st_mtime
        evil=('from pathlib import Path\nPath('+repr(str(marker))+').write_text("bad")\n').encode()
        evil+=b'#'+b' '*(len(original)-len(evil)-1)
        target.write_bytes(evil);os.utime(target,(mtime,mtime));py_compile.compile(str(target),doraise=True)
        target.write_bytes(original);os.utime(target,(mtime,mtime))
    for name,args in [('devin_doctor.py',['--probe']),('devin_compress.py',['prepare'])]:
        run=subprocess.run([sys.executable,str(root/name),*args],env=dict(os.environ),capture_output=True,timeout=20)
        assert run.returncode==0,run.stdout+run.stderr
        assert not marker.exists()


def test_malformed_path_entries_are_dropped(tmp_path):
    import subprocess
    source=tmp_path/'token_estimate.py';source.write_text('VALUE=42');source.chmod(0o600)
    _fixture_manifest(tmp_path)
    code='import sys;sys.path.insert(0,'+repr(str(SCRIPTS))+');import devin_source;sys.path.extend([None,123]);devin_source.activate('+repr(str(tmp_path))+');import token_estimate;assert token_estimate.VALUE==42'
    subprocess.run([sys.executable,'-c',code],check=True,timeout=10)

@pytest.mark.parametrize('part',['directory','leaf'])
def test_foreign_owner_incidental_cache_refused(tmp_path, monkeypatch, part):
    import devin_install as d,types
    from devin_verify import verify_install
    monkeypatch.setenv('HOME',str(tmp_path));d.install();cache=d.state.data_dir()/'plugin/__pycache__';cache.mkdir()
    leaf=cache/'x.pyc';leaf.write_bytes(b'ignored')
    target=cache if part=='directory' else leaf;original=Path.lstat
    def foreign(path,*args,**kwargs):
        info=original(path,*args,**kwargs)
        if path==target:
            return types.SimpleNamespace(st_mode=info.st_mode,st_uid=info.st_uid+1,st_nlink=info.st_nlink)
        return info
    monkeypatch.setattr(Path,'lstat',foreign)
    with pytest.raises(OSError):verify_install()


def test_load_time_digest_detects_changed_source(tmp_path, monkeypatch):
    import devin_install as d,subprocess
    monkeypatch.setenv('HOME',str(tmp_path));d.install();root=d.state.data_dir()/'plugin'
    code='''import sys
from pathlib import Path
root=Path(sys.argv[1]);namespace={}
exec(compile((root/'devin_source.py').read_bytes(),str(root/'devin_source.py'),'exec'),namespace)
namespace['activate'](root)
target=root/'token_estimate.py';target.write_text('VALUE="changed"')
try:import token_estimate
except ImportError:pass
else:raise AssertionError('changed source loaded')
'''
    subprocess.run([sys.executable,'-c',code,str(root)],env=dict(os.environ),check=True,timeout=10)


def test_upgrade_clean_229_install(tmp_path, monkeypatch):
    import devin_install as d,shutil,subprocess
    monkeypatch.setenv('HOME',str(tmp_path))
    fixture=Path(__file__).resolve().parent/'fixtures'/'devin_229_baseline'
    baseline=tmp_path/'baseline'
    # The shipped baseline bytes are stored as a fixture so the upgrade path
    # does not depend on clone depth or object history.
    shutil.copytree(fixture,baseline)
    run=subprocess.run([sys.executable,str(baseline/'skills/token-optimizer/scripts/devin_install.py'),'install'],env=dict(os.environ),capture_output=True,timeout=20)
    assert run.returncode==0,run.stderr
    d.install()
    from devin_verify import verify_install
    assert verify_install()


def test_v3_reachable_closure_is_manifested_and_other_repo_imports_refuse(tmp_path,monkeypatch):
    import devin_install as d,subprocess
    monkeypatch.setenv('HOME',str(tmp_path));d.install();root=d.state.data_dir()/'plugin'
    code='''import sys
from pathlib import Path
root=Path(sys.argv[1]);namespace={}
exec(compile((root/'devin_source.py').read_bytes(),str(root/'devin_source.py'),'exec'),namespace)
namespace['activate'](root)
import plugin_env,refetch_fingerprint
for module in (plugin_env,refetch_fingerprint):
    assert module.__loader__.__class__.__name__=='SourceOnly'
for name in ('archive_result','measure'):
    try:__import__(name)
    except ImportError:pass
    else:raise AssertionError('off-path repo module was not refused')
'''
    run=subprocess.run([sys.executable,'-c',code,str(root)],capture_output=True,timeout=15)
    assert run.returncode==0,run.stderr


def test_v3_compression_never_readds_payload_route():
    assert 'sys.path.insert' not in (SCRIPTS/'bash_compress.py').read_text()


def test_v3_descendant_cache_route_is_removed(tmp_path,monkeypatch):
    import devin_install as d,subprocess,py_compile
    monkeypatch.setenv('HOME',str(tmp_path));d.install();root=d.state.data_dir()/'plugin'
    cache=root/'__pycache__';cache.mkdir();fixture=tmp_path/'incidental_fixture.py';fixture.write_text('VALUE=42')
    py_compile.compile(str(fixture),cfile=str(cache/'incidental_fixture.pyc'),doraise=True)
    from devin_verify import verify_install
    assert verify_install()
    code='''import sys,importlib.util
from pathlib import Path
root=Path(sys.argv[1]);namespace={}
exec(compile((root/'devin_source.py').read_bytes(),str(root/'devin_source.py'),'exec'),namespace)
sys.path.insert(0,str(root/'__pycache__'));namespace['activate'](root)
assert str(root/'__pycache__') not in sys.path
assert importlib.util.find_spec('incidental_fixture') is None
'''
    subprocess.run([sys.executable,'-I','-c',code,str(root)],check=True,timeout=15)


@pytest.mark.parametrize('entry',['devin_doctor.py','devin_compress.py'])
@pytest.mark.parametrize('kind',['symlink','mode','hardlink','digest'])
def test_v3_bootstrap_refuses_unsafe_source_before_exec(tmp_path,monkeypatch,entry,kind):
    import devin_install as d,subprocess
    monkeypatch.setenv('HOME',str(tmp_path));d.install();root=d.state.data_dir()/'plugin';source=root/'devin_source.py'
    if kind=='symlink':
        saved=tmp_path/'saved.py';source.rename(saved);source.symlink_to(saved)
    elif kind=='mode':source.chmod(0o666)
    elif kind=='hardlink':os.link(source,tmp_path/'linked.py')
    else:source.write_bytes(source.read_bytes()+b'\n# changed bootstrap\n')
    run=subprocess.run([sys.executable,str(root/entry),'--help'],capture_output=True,timeout=15)
    assert run.returncode!=0


def test_v3_home_override_normalized_end_to_end(tmp_path,monkeypatch):
    import devin_install as d,subprocess,json
    monkeypatch.setenv('HOME',str(tmp_path));(tmp_path/'segment').mkdir()
    monkeypatch.setenv('TOKEN_OPTIMIZER_DEVIN_HOME',str(tmp_path/'segment/../devin'));d.install()
    assert d.state.home()==tmp_path/'devin'
    run=subprocess.run([sys.executable,str(d.state.data_dir()/'plugin/devin_doctor.py'),'--probe'],capture_output=True,timeout=20)
    assert run.returncode==0,run.stdout+run.stderr
    assert json.loads(run.stdout)['local_probe_passed']


def test_v3_activation_without_manifest_fails_closed(tmp_path):
    import devin_source
    with pytest.raises((OSError,ImportError)):devin_source.activate(tmp_path)


def test_v3_repo_name_policy_covers_current_sources_and_dependencies():
    import ast,devin_source
    repo_names={p.stem for p in SCRIPTS.glob('*.py')}|{p.name for p in SCRIPTS.iterdir() if p.is_dir() and p.name!='__pycache__'}
    assert repo_names==devin_source.REPO_MODULES
    for name in devin_source.PAYLOAD:
        tree=ast.parse((SCRIPTS/name).read_text())
        for node in ast.walk(tree):
            names=[]
            if isinstance(node,ast.Import):names=[alias.name.split('.')[0] for alias in node.names]
            elif isinstance(node,ast.ImportFrom) and node.module:names=[node.module.split('.')[0]]
            for module in set(names)&repo_names:
                assert module in devin_source.ALLOWED_MODULES or (name=='bash_compress.py' and module in {'archive_result','measure'})


@pytest.mark.parametrize('changed',[False,True])
def test_v3_renamed_payload_always_requires_digest(tmp_path,changed):
    import subprocess
    root=tmp_path/'renamed';root.mkdir();_fixture_manifest(root)
    code='''import sys
from pathlib import Path
sys.path.insert(0,SCRIPTS)
import devin_source
root=ROOT;devin_source.activate(root)
if CHANGED:(root/'token_estimate.py').write_text('VALUE=43')
try:
    import token_estimate
except ImportError:
    assert CHANGED
else:
    assert not CHANGED and token_estimate.VALUE==42
'''
    code='SCRIPTS='+repr(str(SCRIPTS))+'\nROOT=__import__("pathlib").Path('+repr(str(root))+')\nCHANGED='+repr(changed)+'\n'+code
    subprocess.run([sys.executable,'-I','-c',code],check=True,timeout=15)


def test_v3_refused_repo_preloads_are_evicted(tmp_path,monkeypatch):
    import devin_install as d,subprocess
    monkeypatch.setenv('HOME',str(tmp_path));d.install();root=d.state.data_dir()/'plugin'
    code='''import sys,types
from pathlib import Path
root=Path(sys.argv[1]);namespace={}
exec(compile((root/'devin_source.py').read_bytes(),str(root/'devin_source.py'),'exec'),namespace)
for name in ('archive_result','measure','measure.child'):sys.modules[name]=types.ModuleType(name)
namespace['activate'](root)
for name in ('archive_result','measure','measure.child'):
    assert name not in sys.modules
    try:__import__(name)
    except ImportError:pass
    else:raise AssertionError('refused preload survives')
'''
    subprocess.run([sys.executable,'-I','-c',code,str(root)],check=True,timeout=15)


def test_v3_upgrade_from_v2_closure_and_failed_refresh_restore(tmp_path,monkeypatch):
    import devin_install as d,json,hashlib
    monkeypatch.setenv('HOME',str(tmp_path));d.install();root=d.state.data_dir()/'plugin';manifest=d.manifest_path()
    record=json.loads(manifest.read_text())
    for name in ('plugin_env.py','refetch_fingerprint.py','devin_hooks.py'):
        (root/name).unlink();del record['files'][str(root/name)]
    manifest.write_text(json.dumps(record))
    original=d._atomic
    def fail(path,content):
        if path==root/'refetch_fingerprint.py':raise OSError('refresh write interrupted')
        return original(path,content)
    monkeypatch.setattr(d,'_atomic',fail)
    with pytest.raises(OSError,match='interrupted'):d.install()
    assert not any((root/name).exists() for name in ('plugin_env.py','refetch_fingerprint.py','devin_hooks.py'))
    assert json.loads(manifest.read_text())==record
    for name,digest in record['files'].items():assert hashlib.sha256(Path(name).read_bytes()).hexdigest()==digest
    monkeypatch.setattr(d,'_atomic',original);d.install()
    from devin_verify import verify_install
    assert verify_install()


def test_v3_interrupted_closure_upgrade_recovers_new_leaves(tmp_path,monkeypatch):
    import devin_install as d,json,base64
    monkeypatch.setenv('HOME',str(tmp_path));d.install();root=d.state.data_dir()/'plugin'
    intended=json.loads(d.manifest_path().read_text());old=dict(intended['files'])
    for name in ('plugin_env.py','refetch_fingerprint.py','devin_hooks.py'):del old[str(root/name)]
    old_manifest=json.dumps({'owner':d.MARKER,'files':old}).encode()
    previous={name:base64.b64encode(Path(name).read_bytes()).decode() for name in old}
    d.manifest_path().write_bytes(old_manifest)
    pending=d.state.data_dir()/'pending-install.json'
    pending.write_text(json.dumps({'owner':d.MARKER,'files':intended['files'],'previous':previous,
                                  'previous_manifest':base64.b64encode(old_manifest).decode()}));pending.chmod(0o600)
    with d._lock():d._recover_pending()
    assert not any((root/name).exists() for name in ('plugin_env.py','refetch_fingerprint.py','devin_hooks.py'))
    d.install()
    from devin_verify import verify_install
    assert verify_install()


@pytest.mark.parametrize('entry',['devin_compress.py','devin_doctor.py'])
@pytest.mark.parametrize('manifest',['missing','wrong_root'])
def test_v4_renamed_installed_entrypoint_refuses(tmp_path,monkeypatch,entry,manifest):
    import devin_install as d,shutil,subprocess
    monkeypatch.setenv('HOME',str(tmp_path));d.install();old=d.state.data_dir()/'plugin';renamed=tmp_path/'renamed'
    renamed.mkdir();root=renamed/'other';shutil.copytree(old,root)
    if manifest=='wrong_root':shutil.copy2(d.manifest_path(),renamed/'install.json')
    target=root/'devin_state.py';target.write_bytes(target.read_bytes()+b'\nRENAMED_FIXTURE=42\n')
    run=subprocess.run([sys.executable,str(root/entry),'--help'],capture_output=True,timeout=15)
    assert run.returncode==2,run.stdout+run.stderr
    assert b'bootstrap manifest gate refused' in run.stderr and b'Traceback' not in run.stderr


def test_v4_refusal_inventory_includes_packages():
    import devin_source
    repo_names={p.stem for p in SCRIPTS.glob('*.py')}|{p.name for p in SCRIPTS.iterdir() if p.is_dir() and p.name!='__pycache__'}
    assert repo_names==devin_source.REPO_MODULES
    with pytest.raises(ImportError):devin_source.SourceOnly(SCRIPTS).find_spec('detectors')


@pytest.mark.parametrize('entry',['devin_compress.py','devin_doctor.py'])
def test_v5_spoofed_source_layout_missing_repo_marker_refuses(tmp_path,monkeypatch,entry):
    import devin_install as d,shutil,subprocess
    monkeypatch.setenv('HOME',str(tmp_path));d.install();fake=tmp_path/'fake';root=fake/'skills/token-optimizer/scripts'
    root.parent.mkdir(parents=True);shutil.copytree(d.state.data_dir()/'plugin',root)
    (root/'devin_install.py').touch();(fake/'devin').mkdir();(fake/'devin/SKILL.md').touch()
    run=subprocess.run([sys.executable,str(root/entry),'--help'],capture_output=True,timeout=15)
    assert run.returncode==2
    assert b'bootstrap manifest gate refused' in run.stderr and b'Traceback' not in run.stderr


@pytest.mark.parametrize('state_file',['install.json','consent.json'])
def test_v5_source_layout_with_installed_state_refuses(tmp_path,monkeypatch,state_file):
    import devin_install as d,shutil,subprocess
    monkeypatch.setenv('HOME',str(tmp_path));d.install();fake=tmp_path/'fake';root=fake/'skills/token-optimizer/scripts'
    root.parent.mkdir(parents=True);shutil.copytree(d.state.data_dir()/'plugin',root)
    (root/'devin_install.py').touch();(fake/'devin').mkdir();(fake/'devin/SKILL.md').touch();(fake/'README.md').touch()
    (root.parent/state_file).write_text('{}')
    run=subprocess.run([sys.executable,str(root/'devin_compress.py'),'--help'],capture_output=True,timeout=15)
    assert run.returncode==2 and b'bootstrap manifest gate refused' in run.stderr and b'Traceback' not in run.stderr


@pytest.mark.parametrize('layout',['scripts','a/scripts','skills/token-optimizer/scripts'])
def test_v5_shallow_layout_clean_manifest_refusal(tmp_path,layout):
    import subprocess,shutil
    root=tmp_path/layout;root.mkdir(parents=True)
    for name in ('devin_compress.py','devin_doctor.py'):shutil.copy2(SCRIPTS/name,root/name)
    for name in ('devin_compress.py','devin_doctor.py'):
        run=subprocess.run([sys.executable,str(root/name),'--help'],capture_output=True,timeout=15)
        assert run.returncode==2 and b'bootstrap manifest gate refused' in run.stderr and b'Traceback' not in run.stderr


@pytest.mark.parametrize('entry',['devin_compress','devin_doctor'])
def test_v5_direct_installed_import_has_explicit_refusal(tmp_path,monkeypatch,entry):
    import devin_install as d,subprocess
    monkeypatch.setenv('HOME',str(tmp_path));d.install();root=d.state.data_dir()/'plugin'
    code='''import sys
sys.path.insert(0,sys.argv[1])
try:__import__(sys.argv[2])
except ImportError as error:
    assert 'bootstrap manifest gate refused' in str(error)
    assert 'command-line use or prior activation' in str(error)
else:raise AssertionError('unsupported installed import succeeded')
'''
    subprocess.run([sys.executable,'-I','-c',code,str(root),entry],check=True,timeout=15)


@pytest.mark.parametrize('tree',['plugins','cowork'])
def test_v5_mirror_entrypoint_clean_manifest_refusal(tree):
    import subprocess
    root=SCRIPTS.parents[2]/tree/'token-optimizer/skills/token-optimizer/scripts'
    for name in ('devin_compress.py','devin_doctor.py'):
        run=subprocess.run([sys.executable,str(root/name),'--help'],capture_output=True,timeout=15)
        assert run.returncode==2 and b'bootstrap manifest gate refused' in run.stderr and b'Traceback' not in run.stderr


@pytest.mark.parametrize('entry',['devin_compress.py','devin_doctor.py'])
@pytest.mark.parametrize('failure',['recursive_manifest','missing_state'])
def test_v6_initial_failures_are_clean_refusals(tmp_path,monkeypatch,entry,failure):
    import devin_install as d,subprocess
    monkeypatch.setenv('HOME',str(tmp_path));d.install();root=d.state.data_dir()/'plugin'
    if failure=='recursive_manifest':
        d.manifest_path().write_bytes(b'['*20000+b'0'+b']'*20000)
        before=d.manifest_path().read_bytes()
    else:
        (root/'devin_state.py').unlink()
        before=None
    run=subprocess.run([sys.executable,str(root/entry),'--help'],capture_output=True,timeout=15)
    assert run.returncode==2 and b'bootstrap manifest gate refused' in run.stderr
    assert b'Traceback' not in run.stderr
    if failure=='recursive_manifest':
        assert d.manifest_path().read_bytes()==before
    else:assert b'manifested payload source missing' in run.stderr


@pytest.mark.parametrize('entry',['devin_state.py','devin_verify.py'])
def test_v6_internal_modules_refuse_direct_execution(tmp_path,monkeypatch,entry):
    import devin_install as d,subprocess
    monkeypatch.setenv('HOME',str(tmp_path));d.install();root=d.state.data_dir()/'plugin';marker=tmp_path/'INTERNAL_APPENDED_CODE'
    target=root/entry;target.write_bytes(target.read_bytes()+('\nfrom pathlib import Path\nPath('+repr(str(marker))+').touch()\n').encode())
    run=subprocess.run([sys.executable,str(target),'--help'],capture_output=True,timeout=15)
    assert run.returncode==2 and b'internal module requires an activated entrypoint' in run.stderr
    assert b'Traceback' not in run.stderr and not marker.exists()


@pytest.mark.parametrize('entry',[
    'devin_state.py','devin_verify.py','devin_source.py','bash_compress.py','bash_whitelist.py',
    'credential_patterns.py','token_estimate.py','command_filters.py','runtime_env.py','py_trust.py',
    'plugin_env.py','refetch_fingerprint.py'])
def test_v7_every_installed_internal_refuses_direct_execution(tmp_path,monkeypatch,entry):
    import devin_install as d,subprocess
    monkeypatch.setenv('HOME',str(tmp_path));d.install();root=d.state.data_dir()/'plugin'
    run=subprocess.run([sys.executable,str(root/entry),'--help'],capture_output=True,timeout=15)
    assert run.returncode==2 and b'internal module requires an activated entrypoint' in run.stderr
    assert b'Traceback' not in run.stderr


def test_v7_renderer_commits_installed_bytes_after_future_imports(tmp_path,monkeypatch):
    import devin_install as d,ast,hashlib,json
    monkeypatch.setenv('HOME',str(tmp_path));d.install();record=json.loads(d.manifest_path().read_text())
    for name in d.PAYLOAD:
        original=(SCRIPTS/name).read_bytes();rendered=d._render_payload(name)
        assert rendered==d._render_payload(name)
        installed=d.state.data_dir()/'plugin'/name
        assert installed.read_bytes()==rendered
        assert record['files'][str(installed)]==hashlib.sha256(rendered).hexdigest()
        assert (SCRIPTS/name).read_bytes()==original
        if name in ('devin_compress.py','devin_doctor.py'):assert rendered==original;continue
        nodes=ast.parse(rendered).body
        first=next(n for n in nodes if not (isinstance(n,ast.Expr) and isinstance(n.value,ast.Constant) and isinstance(n.value.value,str)) and not (isinstance(n,ast.ImportFrom) and n.module=='__future__'))
        assert isinstance(first,ast.If) and "__main__" in ast.unparse(first.test)


@pytest.mark.parametrize('entry',['devin_compress.py','devin_doctor.py'])
def test_v7_recursive_consent_has_clean_refusal(tmp_path,monkeypatch,entry):
    import devin_install as d,subprocess
    monkeypatch.setenv('HOME',str(tmp_path));d.install();root=d.state.data_dir()/'plugin'
    (d.state.data_dir()/'consent.json').write_bytes(b'['*20000+b'0'+b']'*20000)
    args=['--probe'] if entry=='devin_doctor.py' else ['prepare']
    run=subprocess.run([sys.executable,str(root/entry),*args],capture_output=True,timeout=15)
    assert run.returncode in (1,2) and b'Traceback' not in run.stderr
    assert b'consent' in (run.stdout+run.stderr).lower()


@pytest.mark.parametrize('kind',['missing','digest','mode'])
def test_v7_doctor_missing_or_changed_verify_has_clean_refusal(tmp_path,monkeypatch,kind):
    import devin_install as d,subprocess
    monkeypatch.setenv('HOME',str(tmp_path));d.install();root=d.state.data_dir()/'plugin';target=root/'devin_verify.py'
    if kind=='missing':target.unlink()
    elif kind=='digest':target.write_bytes(target.read_bytes()+b'\n# changed\n')
    else:target.chmod(0o644)
    run=subprocess.run([sys.executable,str(root/'devin_doctor.py'),'--probe'],capture_output=True,timeout=15)
    assert run.returncode==2 and b'Traceback' not in run.stderr
    reason={'missing':b'manifested payload source missing','digest':b'payload source differs from install manifest','mode':b'private regular payload source required'}[kind]
    assert reason in run.stderr


@pytest.mark.parametrize('content,reason',[
    (b'{',b'Expecting property name'),(b'\xff',b'codec'),
    (b'[]',b'invalid bootstrap manifest'),(b'{"owner":"devin-output-filter-v1","files":[]}',b'invalid bootstrap manifest')])
def test_v7_manifest_json_classes_report_specific_reasons(tmp_path,monkeypatch,content,reason):
    import devin_install as d,subprocess
    monkeypatch.setenv('HOME',str(tmp_path));d.install();root=d.state.data_dir()/'plugin';d.manifest_path().write_bytes(content)
    for entry in ('devin_compress.py','devin_doctor.py'):
        run=subprocess.run([sys.executable,str(root/entry),'--help'],capture_output=True,timeout=15)
        assert run.returncode==2 and b'bootstrap manifest gate refused' in run.stderr
        assert reason in run.stderr and b'Traceback' not in run.stderr


def test_v7_shared_checkout_cli_and_rendering_do_not_mutate_sources(tmp_path,monkeypatch):
    import devin_install as d,subprocess,hashlib
    before={name:hashlib.sha256((SCRIPTS/name).read_bytes()).hexdigest() for name in d.PAYLOAD}
    monkeypatch.setenv('HOME',str(tmp_path));d.install()
    assert before=={name:hashlib.sha256((SCRIPTS/name).read_bytes()).hexdigest() for name in d.PAYLOAD}
    fixture=tmp_path/'listing';fixture.mkdir();(fixture/'VISIBLE_SHARED_CLI').touch()
    run=subprocess.run([sys.executable,str(SCRIPTS/'bash_compress.py'),'ls',str(fixture)],capture_output=True,timeout=15)
    assert run.returncode==0 and b'VISIBLE_SHARED_CLI' in run.stdout


@pytest.mark.parametrize('action',['install','uninstall'])
@pytest.mark.parametrize('leaf',['install.json','pending-install.json'])
def test_v8_installer_recursive_metadata_refuses_cleanly(tmp_path,monkeypatch,action,leaf):
    import devin_install as d,subprocess
    monkeypatch.setenv('HOME',str(tmp_path));d.install();target=d.state.data_dir()/leaf
    target.write_bytes(b'['*20000+b']'*20000);target.chmod(0o600);before=target.read_bytes()
    run=subprocess.run([sys.executable,str(SCRIPTS/'devin_install.py'),action],capture_output=True,timeout=15)
    # Deep nesting may parse or refuse depending on the interpreter's JSON
    # limits; either way it is a clean refusal and the bytes stay untouched.
    assert run.returncode in (1,2) and (run.stdout+run.stderr)
    assert b'Traceback' not in run.stderr and target.read_bytes()==before


@pytest.mark.parametrize('source',[b'"""doc"""; import os\n',b'from __future__ import annotations; import os\n'])
def test_v8_renderer_refuses_prefix_line_side_effects(tmp_path,monkeypatch,source):
    import devin_install as d
    (tmp_path/'fixture.py').write_bytes(source);monkeypatch.setattr(d,'SCRIPTS',tmp_path)
    with pytest.raises((OSError,ValueError),match='prefix'):d._render_payload('fixture.py')


def test_v8_one_installed_guard_per_internal(tmp_path,monkeypatch):
    import devin_install as d
    monkeypatch.setenv('HOME',str(tmp_path));d.install()
    for name in d.PAYLOAD:
        if name in ('devin_compress.py','devin_doctor.py'):continue
        content=(d.state.data_dir()/'plugin'/name).read_text()
        assert content.count("if __name__ == '__main__':")==1,name


@pytest.mark.parametrize('source',[b'',b'VALUE=42\n',b'"""doc"""\r\nfrom __future__ import annotations\r\nVALUE=42\r\n'])
def test_v8_renderer_handles_empty_docless_and_crlf(tmp_path,monkeypatch,source):
    import devin_install as d,ast
    (tmp_path/'fixture.py').write_bytes(source);monkeypatch.setattr(d,'SCRIPTS',tmp_path)
    result=d._render_payload('fixture.py');assert result==d._render_payload('fixture.py')
    nodes=ast.parse(result).body
    executable=[n for n in nodes if not (isinstance(n,ast.Expr) and isinstance(n.value,ast.Constant) and isinstance(n.value.value,str)) and not (isinstance(n,ast.ImportFrom) and n.module=='__future__')]
    assert isinstance(executable[0],ast.If)


@pytest.mark.parametrize('operation',['seal','recover','filter'])
@pytest.mark.parametrize('value',[[],None,42,'text',{'stdout':[],'stderr':[]},
    {'stdout':[True,1],'stderr':[1,1]}, {'stdout':[-1,1],'stderr':[1,1]},
    {'stdout':[1],'stderr':[1,1]}, {'stdout':[1,2,3],'stderr':[1,1]},
    {'stdout':['1',1],'stderr':[1,1]}, {'stdout':[1.0,1],'stderr':[1,1]},
    {'stdout':[[1],1],'stderr':[1,1]}, {'stdout':[1,1]},
    {'stdout':[1,1],'stderr':[1,1],'extra':0}])
def test_v9_prepared_shape_has_clean_refusal(tmp_path,monkeypatch,operation,value):
    import devin_install as d,devin_state as s,devin_compress as c,subprocess,json
    monkeypatch.setenv('HOME',str(tmp_path));d.install();capture=s.prepare_capture()
    (capture/'stdout').write_bytes(b'repeated line\n'*1000)
    if operation!='seal':c.seal_capture(capture,0)
    (capture/'.prepared').write_text(json.dumps(value))
    before={p.name:p.read_bytes() for p in capture.iterdir()}
    root=d.state.data_dir()/'plugin'
    if operation=='seal':args=['seal','--capture',str(capture),'--exit-code','0']
    elif operation=='recover':args=['recover','--capture-id',capture.name,'--exit-code','0']
    else:args=['--stdout',str(capture/'stdout'),'--stderr',str(capture/'stderr'),'--exit-code','0','--format','generic','--session','v9']
    run=subprocess.run([sys.executable,str(root/'devin_compress.py'),*args],capture_output=True,timeout=15)
    assert run.returncode==2 and b'Traceback' not in run.stderr
    assert b'invalid prepared capture identity shape' in run.stderr.lower()
    assert before=={p.name:p.read_bytes() for p in capture.iterdir()}
    if operation=='seal':assert not (capture/'complete.json').exists()


@pytest.mark.parametrize('value',[[],None,42,'text',{'stdout':[True,1],'stderr':[1,1]}])
def test_v9_other_metadata_shapes_refuse_without_tracebacks(tmp_path,monkeypatch,value):
    import devin_install as d,subprocess,json
    monkeypatch.setenv('HOME',str(tmp_path));d.install();root=d.state.data_dir()/'plugin'
    for leaf in ('install.json','pending-install.json','consent.json'):
        target=d.state.data_dir()/leaf;before=target.read_bytes() if target.exists() else None
        target.write_text(json.dumps(value));target.chmod(0o600)
        args=[sys.executable,str(SCRIPTS/'devin_install.py'),'install'] if leaf!='consent.json' else [sys.executable,str(root/'devin_compress.py'),'prepare']
        run=subprocess.run(args,capture_output=True,timeout=15)
        assert run.returncode==(2 if leaf=='consent.json' else 1) and b'Traceback' not in run.stderr
        if before is None:target.unlink()
        else:target.write_bytes(before)


@pytest.mark.parametrize('invalid',[123,None,[],{},True])
def test_v10_pending_previous_base64_shape_refuses_cleanly(tmp_path,monkeypatch,invalid):
    import devin_install as d,json,subprocess
    monkeypatch.setenv('HOME',str(tmp_path));d.install();record=json.loads(d.manifest_path().read_text())
    # A pending update whose published manifest is still the old generation.
    old=dict(record);old['files']=dict(record['files']);first=next(iter(old['files']));old['files'][first]='0'*64
    d.manifest_path().write_text(json.dumps(old))
    pending=d.state.data_dir()/'pending-install.json'
    pending.write_text(json.dumps({'owner':d.MARKER,'files':record['files'],
        'previous':{name:invalid for name in record['files']},'previous_manifest':'AA=='}));pending.chmod(0o600)
    before={p: p.read_bytes() for p in d.state.data_dir().rglob('*') if p.is_file()}
    run=subprocess.run([sys.executable,str(SCRIPTS/'devin_install.py'),'install'],capture_output=True,timeout=15)
    assert run.returncode==1 and b'invalid refresh recovery encoding' in run.stderr
    assert b'Traceback' not in run.stderr
    assert all(path.read_bytes()==content for path,content in before.items())


def _v11_pending_fixture(tmp_path,monkeypatch):
    import devin_install as d,json,base64,hashlib
    monkeypatch.setenv('HOME',str(tmp_path));d.install()
    current=json.loads(d.manifest_path().read_text())
    old_bytes={name:Path(name).read_bytes() for name in current['files']}
    changed=str(d.state.data_dir()/'plugin/token_estimate.py')
    old_bytes[changed]+=b'\n# prior generation fixture\n'
    old={'owner':d.MARKER,'files':{name:hashlib.sha256(content).hexdigest() for name,content in old_bytes.items()}}
    old_manifest=json.dumps(old,sort_keys=True).encode();d.manifest_path().write_bytes(old_manifest)
    record={'owner':d.MARKER,'files':current['files'],
            'previous':{name:base64.b64encode(content).decode() for name,content in old_bytes.items()},
            'previous_manifest':base64.b64encode(old_manifest).decode()}
    return d,record


def _v11_refuse_preserving(d,record,reason=None):
    import json,subprocess
    pending=d.state.data_dir()/'pending-install.json';pending.write_text(json.dumps(record));pending.chmod(0o600)
    before={p:p.read_bytes() for p in d.state.data_dir().rglob('*') if p.is_file()}
    before[d.skill_dir()/'SKILL.md']=(d.skill_dir()/'SKILL.md').read_bytes()
    run=subprocess.run([sys.executable,str(SCRIPTS/'devin_install.py'),'install'],capture_output=True,timeout=15)
    assert run.returncode==1 and b'Traceback' not in run.stderr,run.stdout+run.stderr
    assert b'Devin install refused' in run.stderr
    if reason is not None:assert reason in run.stderr
    assert all(p.exists() and p.read_bytes()==content for p,content in before.items())
    assert all(p.exists() for p in before)


@pytest.mark.parametrize('field',['leaf','manifest'])
@pytest.mark.parametrize('bad',[123,None,[],{},True,'!!!'])
def test_v11_single_bad_encoding_preserves_all_bytes(tmp_path,monkeypatch,field,bad):
    d,record=_v11_pending_fixture(tmp_path,monkeypatch)
    if field=='manifest':record['previous_manifest']=bad
    else:
        names=list(record['previous']);record['previous'][names[len(names)//2]]=bad
    _v11_refuse_preserving(d,record,b'base64' if bad=='!!!' else b'invalid refresh recovery encoding')


@pytest.mark.parametrize('bad',['junk_leaf','junk_manifest','wrong_owner','wrong_keys','wrong_digest','different_old_manifest'])
def test_v11_decoded_rollback_content_must_match_old_commitments(tmp_path,monkeypatch,bad):
    import json,base64
    d,record=_v11_pending_fixture(tmp_path,monkeypatch)
    if bad=='junk_leaf':record['previous'][str(d.state.data_dir()/'plugin/token_estimate.py')]=base64.b64encode(b'junk').decode()
    elif bad=='junk_manifest':record['previous_manifest']=base64.b64encode(b'junk').decode()
    else:
        old=json.loads(base64.b64decode(record['previous_manifest']))
        if bad=='wrong_owner':old['owner']='foreign'
        elif bad=='wrong_keys':old['files'].pop(next(iter(old['files'])))
        elif bad=='wrong_digest':old['files'][next(iter(old['files']))]='invalid'
        else:old['different']=True
        record['previous_manifest']=base64.b64encode(json.dumps(old).encode()).decode()
    reason=b'invalid previous ownership commitments' if bad in ('wrong_owner','wrong_keys','wrong_digest') else None
    _v11_refuse_preserving(d,record,reason)


@pytest.mark.parametrize('bad',[[],42,{},'text'])
def test_v11_previous_shape_refuses_preserving_bytes(tmp_path,monkeypatch,bad):
    d,record=_v11_pending_fixture(tmp_path,monkeypatch);record['previous']=bad
    _v11_refuse_preserving(d,record,b'invalid refresh recovery record')


@pytest.mark.parametrize('position',['stdout','stderr'])
@pytest.mark.parametrize('bad',[42,None,{'a':1,'b':2}])
def test_v11_prepared_nonlist_identity_refuses_preserving_bytes(tmp_path,monkeypatch,position,bad):
    import devin_install as d,devin_state as s,subprocess,json
    monkeypatch.setenv('HOME',str(tmp_path));d.install();capture=s.prepare_capture()
    prepared=capture/'.prepared';value=json.loads(prepared.read_text());value[position]=bad;prepared.write_text(json.dumps(value))
    before={p.name:p.read_bytes() for p in capture.iterdir()}
    run=subprocess.run([sys.executable,str(d.state.data_dir()/'plugin/devin_compress.py'),'seal','--capture',str(capture),'--exit-code','0'],capture_output=True,timeout=15)
    assert run.returncode==2 and b'invalid prepared capture identity shape' in run.stderr and b'Traceback' not in run.stderr
    assert before=={p.name:p.read_bytes() for p in capture.iterdir()}


def test_v11_bad_manifest_leaves_added_closure_intact(tmp_path,monkeypatch):
    import json,base64
    d,record=_v11_pending_fixture(tmp_path,monkeypatch)
    old=json.loads(base64.b64decode(record['previous_manifest']))
    for name in ('plugin_env.py','refetch_fingerprint.py','devin_hooks.py'):
        key=str(d.state.data_dir()/'plugin'/name);del old['files'][key];del record['previous'][key]
    d.manifest_path().write_text(json.dumps(old))
    record['previous_manifest']='!!!'
    _v11_refuse_preserving(d,record,b'base64')
    assert all((d.state.data_dir()/'plugin'/name).exists() for name in ('plugin_env.py','refetch_fingerprint.py','devin_hooks.py'))

@pytest.mark.parametrize('entry,args',[('devin_compress.py',['prepare']),('devin_doctor.py',['--probe'])])
@pytest.mark.parametrize('module',['json','argparse','hashlib','pathlib','subprocess'])
@pytest.mark.parametrize('isolated',[False,True])
def test_v12_stdlib_shadow_never_executes(tmp_path,monkeypatch,entry,args,module,isolated):
    import devin_install as d,subprocess,json
    monkeypatch.setenv('HOME',str(tmp_path));d.install()
    poison=tmp_path/'poison';poison.mkdir();marker=tmp_path/'executed'
    (poison/(module+'.py')).write_text('open('+repr(str(marker))+',"w").write("executed")\nraise RuntimeError("shadow ran")\n')
    env=dict(os.environ,PYTHONPATH=str(poison))
    run=subprocess.run([sys.executable,*(['-I'] if isolated else []),str(d.state.data_dir()/'plugin'/entry),*args],env=env,capture_output=True,timeout=20)
    assert not marker.exists(),run.stdout+run.stderr
    assert run.returncode==0,run.stdout+run.stderr
    if entry=='devin_doctor.py':assert json.loads(run.stdout)['local_probe_passed'] is True


def test_v12_doctor_child_uses_isolated_startup(tmp_path,monkeypatch):
    import devin_install as d,subprocess,json
    monkeypatch.setenv('HOME',str(tmp_path));d.install()
    poison=tmp_path/'poison';poison.mkdir();marker=tmp_path/'startup-executed'
    (poison/'sitecustomize.py').write_text('open('+repr(str(marker))+',"w").write("executed")\n')
    run=subprocess.run([sys.executable,'-I',str(d.state.data_dir()/'plugin/devin_doctor.py'),'--probe'],env=dict(os.environ,PYTHONPATH=str(poison)),capture_output=True,timeout=20)
    assert not marker.exists(),run.stdout+run.stderr
    assert run.returncode==0 and json.loads(run.stdout)['local_probe_passed'] is True


@pytest.mark.parametrize('decoded',[[],42,None,'scalar','list_files'])
def test_v12_decoded_manifest_schema_refuses_preserving_bytes(tmp_path,monkeypatch,decoded):
    import base64,json
    d,record=_v11_pending_fixture(tmp_path,monkeypatch)
    if decoded=='list_files':decoded={'owner':d.MARKER,'files':list(record['previous'])}
    record['previous_manifest']=base64.b64encode(json.dumps(decoded).encode()).decode()
    _v11_refuse_preserving(d,record,b'invalid previous ownership commitments')


@pytest.mark.parametrize('kind',['digest_swap','one_byte_manifest'])
def test_v12_exact_commitment_boundaries(tmp_path,monkeypatch,kind):
    import base64
    d,record=_v11_pending_fixture(tmp_path,monkeypatch)
    if kind=='digest_swap':
        names=list(record['previous'])[:2]
        assert record['previous'][names[0]]!=record['previous'][names[1]]
        record['previous'][names[0]],record['previous'][names[1]]=record['previous'][names[1]],record['previous'][names[0]]
        reason=b'previous rollback source differs from ownership commitments'
    else:
        original=base64.b64decode(record['previous_manifest'])
        # One whitespace byte changes the commitment without changing JSON meaning.
        changed=original.replace(b' ',b'\t',1);assert len(changed)==len(original) and sum(a!=b for a,b in zip(original,changed))==1
        record['previous_manifest']=base64.b64encode(changed).decode()
        reason=b'previous ownership differs from preserved manifest'
    _v11_refuse_preserving(d,record,reason)


def test_v12_string_identity_refuses_preserving_bytes(tmp_path,monkeypatch):
    import devin_install as d,devin_state as s,subprocess,json
    monkeypatch.setenv('HOME',str(tmp_path));d.install();capture=s.prepare_capture()
    prepared=capture/'.prepared';value=json.loads(prepared.read_text());value['stdout']='ab';prepared.write_text(json.dumps(value))
    before={p.name:p.read_bytes() for p in capture.iterdir()}
    run=subprocess.run([sys.executable,str(d.state.data_dir()/'plugin/devin_compress.py'),'seal','--capture',str(capture),'--exit-code','0'],capture_output=True,timeout=15)
    assert run.returncode==2 and b'invalid prepared capture identity shape' in run.stderr and b'Traceback' not in run.stderr
    assert before=={p.name:p.read_bytes() for p in capture.iterdir()}

@pytest.mark.parametrize('location',['installed','source'])
@pytest.mark.parametrize('entry,args',[('devin_compress.py',['prepare']),('devin_doctor.py',['--probe'])])
def test_v13_startup_os_shadow_isolated_positive_control(tmp_path,monkeypatch,location,entry,args):
    import devin_install as d,subprocess,json
    monkeypatch.setenv('HOME',str(tmp_path));d.install()
    poison=tmp_path/'poison';poison.mkdir();marker=tmp_path/'os-executed'
    startup_marker=tmp_path/'sitecustomize-executed'
    (poison/'sitecustomize.py').write_text('open('+repr(str(startup_marker))+',"w").write("executed")\n')
    (poison/'os.py').write_text('open('+repr(str(marker))+',"w").write("executed")\nraise RuntimeError("os startup shadow")\n')
    root=d.state.data_dir()/'plugin' if location=='installed' else SCRIPTS
    run=subprocess.run([sys.executable,'-I',str(root/entry),*args],env=dict(os.environ,PYTHONPATH=str(poison)),capture_output=True,timeout=20)
    assert not marker.exists() and not startup_marker.exists(),run.stdout+run.stderr
    assert run.returncode==0,run.stdout+run.stderr
    if entry=='devin_doctor.py':assert json.loads(run.stdout)['local_probe_passed'] is True


@pytest.mark.parametrize('entry',['devin_compress.py','devin_doctor.py'])
def test_v13_plain_startup_os_shadow_characterizes_unsupported_path(tmp_path,monkeypatch,entry):
    import devin_install as d,subprocess
    monkeypatch.setenv('HOME',str(tmp_path));d.install()
    poison=tmp_path/'poison';poison.mkdir();marker=tmp_path/'os-executed'
    (poison/'os.py').write_text('open('+repr(str(marker))+',"w").write("executed")\nraise RuntimeError("os startup shadow")\n')
    run=subprocess.run([sys.executable,str(d.state.data_dir()/'plugin'/entry),'--help'],env=dict(os.environ,PYTHONPATH=str(poison)),capture_output=True,timeout=20)
    # CPython 3.10 loads os during site startup before any script can re-exec.
    # Newer frozen-os builds differ; neither behavior makes plain use supported.
    if sys.version_info[:2]==(3,10):
        assert marker.exists() and run.returncode!=0
        assert b'Failed to import the site module' in run.stderr
    else:
        assert not marker.exists() and run.returncode==0,run.stdout+run.stderr


def test_v13_supported_examples_require_startup_isolation():
    import re
    def refuse_contradictions(content):
        # Exclude shell comments and literal cat output, not runnable shell heredocs.
        visible=[]
        literal_end=None
        for line in content.splitlines():
            if literal_end is not None:
                if line.strip()==literal_end:literal_end=None
                continue
            literal=re.search(r"^\s*cat\s+<<\s*(['\"])([A-Za-z_]\w*)\1\s*$",line)
            if literal:
                literal_end=literal.group(2)
                continue
            if line.lstrip().startswith('#') and not re.match(r'^#{1,6}\s',line):continue
            # Markdown headings survive; shell comments are handled by command parser.
            if line.lstrip().startswith('# ') and re.search(r'\bpython[23]?\b',line):continue
            visible.append(line)
        content='\n'.join(visible)
        # Invocation advice is closed by class, not a growing modal-word list.
        neutral = {
            '`prepare`, `seal` and `recover` use their required source commitments and capture/consent checks, but do not verify the entire compression closure; filtering and doctor readiness do.',
            'Run the installed filter explicitly:',  # Followed by a checked -I code example.
        }
        verb = r'\b(?:run|running|runs|invoke|invoked|invoking|invokes|launch|launched|launching|launches|execute|executed|executing|executes|call|called|calling|use|used|using|uses|start|started|starting|starts)\b'
        target = r'\b(?:doctor|filter|devin_doctor|devin_compress)\.py\b|\bplain\s+python\b|\bthe\s+(?:installed\s+)?(?:filter|doctor)\b|\bdoctor/filter\b'
        # Join ordinary shell continuations and resolve simple example assignments.
        logical=re.sub(r'\\\r?\n', '', content)
        variables={}
        fenced=False
        for line in logical.splitlines():
            if line.strip().startswith('```'):
                fenced=not fenced
                continue
            for assignment in re.finditer(r'(?:^|[;\s])([A-Za-z_]\w*)=([^;\n]+)',line):
                variables[assignment.group(1)]=assignment.group(2).strip().strip('"\'')
            expanded=line
            for _ in range(len(variables)+1):
                updated=re.sub(r'\$\{([A-Za-z_]\w*)\}|\$([A-Za-z_]\w*)',lambda m:variables.get(m.group(1) or m.group(2),m.group(0)),expanded)
                if updated==expanded:break
                expanded=updated
            line=expanded
            # Fence labels and variable assignments are not invocations.
            positions=r'(?:^|[;|&`]|\$\()' if fenced or re.match(r'^\s*\w+=',line) else r'(?:^|[;|&]|\$\()'
            command=re.search(positions+r'\s*(?:[A-Za-z_]\w*=(?:"[^"]*"|\'[^\']*\'|[^\s;|&`]+)\s+)*(?:(?:env|command|nice|sudo|xargs|setsid|nohup|timeout)\b(?:(?!\bpython[23]?\b)[^;|&`\n])*?\s+)*python[23]?\b(?P<args>[^;|&`\n]*)',line)
            if command and re.search(r'(?:doctor|filter|devin_doctor|devin_compress)\.py|\$(?:FILTER|DOCTOR)',command.group('args'),re.I):
                script=re.search(r'(?:doctor|filter|devin_doctor|devin_compress)\.py|\$(?:FILTER|DOCTOR)',command.group('args'),re.I)
                before_script=command.group('args')[:script.start()]
                assert re.search(r'(?:^|\s)-I(?:\s|$)',before_script),line
        for sentence in re.split(r'(?<=[.!?])\s+|\n', content):
            sentence=sentence.strip()
            if sentence in neutral:
                continue
            historical=re.search(r'\b(?:was\s+(?:used|run|invoked)|previously\s+invoked)\b',sentence,re.I)
            guidance=re.search(r'\b(?:you|please|should|may|can|must|go\s+ahead|feel\s+free)\b',sentence,re.I)
            if historical and not guidance:continue
            if re.search(verb,sentence,re.I) and re.search(target,sentence,re.I):
                negative = re.search(r'\b(?:unsupported|not\s+supported|rejected|never|cannot|do\s+not|must\s+not)\b',sentence,re.I)
                assert negative or ('-I' in sentence and not re.search(r'\b(?:without|omit|drop|remove|optional|unnecessary)\b',sentence,re.I)), sentence
        # Catch accidental conflicting guidance, not arbitrary malicious prose.
        for clause in re.split(r'[.!?;\n]', content):
            if re.search(r'\bplain\b.{0,50}\b(?:python|invocation|shebang)\b|\bdirect\s+shebang\b', clause, re.I):
                assert not re.search(r'\b(?:supported|safe|recommended)\b', clause, re.I), clause
                assert not re.search(r'\bnot\s+unsupported\b', clause, re.I), clause
        assert not re.search(r'\b(?:feel\s+free\s+to|just)\s+(?:run|invoke|execute)\s+[`\"]?python[23]?\s+(?:doctor|filter|devin_doctor|devin_compress)\.py', content, re.I)
        assert not re.search(r'\b(?:are\s+free\s+to|may|can|should)\s+(?:invoke|run|execute)\s+(?:the\s+)?(?:doctor|filter)(?:\.py)?\s+directly\b', content, re.I)
        assert not re.search(r'\b(?:recommend|recommended)\s+(?:using|use\s+of)\s+plain\s+python\b', content, re.I)
        assert not re.search(r'\b(?:may|can|should)\s+(?:invoke|run|execute)\s+(?:the\s+\w+\s+)?directly\b', content, re.I)
        assert not re.search(r'\b(?:safe|supported|recommended)\s+to\s+(?:run|invoke|execute)\s+[`\"]?python[23]?(?:\s+[^.\n]*)?\s+(?:doctor|filter|devin_doctor|devin_compress)\.py', content, re.I)
        assert not re.search(r'\b(?:omit|drop|remove)\s+(?:the\s+)?[`\"]?-I', content, re.I)
        assert not re.search(r'-I[`\"]?\s+(?:is\s+)?(?:optional|unnecessary|not\s+required)', content, re.I)
    for name in ('devin/README.md','docs/devin.md'):
        content=(SCRIPTS.parents[2]/name).read_text()
        guidance=content.split('## Installed import boundary\n',1)[1]
        guidance=re.split(r'^#{1,6}\s',guidance, maxsplit=1, flags=re.M)[0]
        refuse_contradictions(content)
        assert 'Supported doctor/filter CLI use requires a trusted Python interpreter launched with `-I` from the first startup, including source-development CLI commands.' in guidance
        assert "Plain Python invocation is unsupported: the entrypoints' early isolated re-exec is defense-in-depth only and cannot protect imports that Python performs before the script begins." in guidance
        assert 'On Python 3.10, a poisoned `PYTHONPATH` containing `os.py` can execute during startup and prevent the script from running at all; the tested Python 3.12 build uses frozen `os`, but plain invocation is still unsupported.' in guidance
        for literal in ('os.py','sitecustomize','.pth','PYTHONHOME'):assert literal in content
        assert 'python3 "$FILTER"' not in content
        assert 'measure.py devin-doctor' not in content
        assert 'python3 -I "$DOCTOR" --probe' in content
    skill=(SCRIPTS.parents[2]/'devin/SKILL.md').read_text()
    introduction=re.split(r'^#{1,6}\s|^```',skill,maxsplit=1,flags=re.M)[0]
    refuse_contradictions(skill)
    assert 'Keep `-I` in every filter command. It is required from the first trusted Python startup; plain invocation and direct shebang execution are unsupported.' in introduction
    for line in skill.splitlines():
        if '@FILTER@' in line:assert '@PYTHON@ -I @FILTER@' in line
