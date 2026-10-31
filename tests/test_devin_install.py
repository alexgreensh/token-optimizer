from pathlib import Path
import sys
import os
import stat
import pytest
pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Native Windows unsupported by production design (installer refuses os.name != posix); tests gated accordingly.')

SCRIPTS=Path(__file__).resolve().parents[1]/'skills/token-optimizer/scripts'
sys.path.insert(0,str(SCRIPTS))

def test_install_preserves_config_and_foreign_skills(tmp_path, monkeypatch):
    import devin_install as d
    monkeypatch.setenv('HOME',str(tmp_path))
    config=tmp_path/'.config/devin/config.json'; config.parent.mkdir(parents=True)
    config.write_text('{"permissions":{"deny":["exec"]}}')
    before=config.read_bytes()
    d.install()
    assert config.read_bytes()==before
    skill=config.parent/'skills/token-optimizer-output/SKILL.md'
    assert skill.is_file()
    assert 'allowed-tools' not in skill.read_text()
    assert 'triggers: [user]' in skill.read_text()
    d.install()
    d.uninstall()
    assert config.read_bytes()==before
    assert not skill.exists()

import pytest

@pytest.mark.parametrize('mode',['foreign','symlink','edited'])
def test_installer_refuses_collision(tmp_path,monkeypatch,mode):
    import devin_install as d
    import devin_state as s
    monkeypatch.setenv('HOME',str(tmp_path))
    skill=s.home()/'skills/token-optimizer-output'
    if mode=='edited':
        d.install();(skill/'SKILL.md').write_text('user edit')
    else:
        skill.parent.mkdir(parents=True)
        if mode=='foreign':skill.mkdir();(skill/'SKILL.md').write_text('foreign')
        else:skill.symlink_to(tmp_path/'elsewhere')
    with pytest.raises(OSError):d.install()
    if mode=='edited':
        with pytest.raises(OSError):d.uninstall()
        assert (skill/'SKILL.md').read_text()=='user edit'

def test_dry_run_no_writes(tmp_path,monkeypatch):
    import devin_install as d
    monkeypatch.setenv('HOME',str(tmp_path));d.install(dry_run=True)
    assert list(tmp_path.iterdir())==[]


def test_symlinked_skill_dir_refusal_says_symlink(tmp_path, monkeypatch):
    """A skill dir swapped for a symlink after install must be refused as a
    symlink, the way the previous release reported it -- not with the
    different-version/home-spelling advice, which does not fit this cause."""
    import devin_install as d
    import devin_state as s
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    skill = s.home() / 'skills/token-optimizer-output'
    parked = tmp_path / 'parked-real'
    skill.rename(parked)
    skill.symlink_to(parked)
    for action in (d.install, d.uninstall):
        with pytest.raises(OSError) as info:
            action()
        assert 'symlink' in str(info.value), str(info.value)
    d.uninstall(purge=True)
    assert skill.is_symlink()


def test_owned_leaf_as_directory_names_the_path(tmp_path, monkeypatch):
    """An owned file replaced by a directory must refuse with its path; a bare
    file descriptor number in the message is cryptic and unactionable."""
    import devin_install as d
    import devin_state as s
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    target = s.data_dir() / 'plugin' / 'devin_state.py'
    target.unlink()
    target.mkdir()
    for action in (d.install, d.uninstall):
        with pytest.raises(OSError) as info:
            action()
        assert str(target) in str(info.value), str(info.value)

@pytest.mark.parametrize('name',['../outside','relative','/tmp/outside'])
def test_invalid_home_override(tmp_path,monkeypatch,name):
    import devin_state as s
    monkeypatch.setenv('HOME',str(tmp_path));monkeypatch.setenv('TOKEN_OPTIMIZER_DEVIN_HOME',name)
    with pytest.raises(OSError):s.home()

def test_genuine_devin_cli_discovery(tmp_path,monkeypatch):
    """Run the real CLI only with explicit opt-in; never starts a model/session."""
    import os,shutil,subprocess
    import devin_install as d
    if os.environ.get('DEVIN_TEST_BINARY', '').strip().lower() not in {'1', 'true', 'yes', 'on'}:
        pytest.skip('set DEVIN_TEST_BINARY=true to opt in to real CLI discovery')
    binary=shutil.which('devin')
    if not binary:pytest.skip('devin CLI on PATH required for real discovery')
    monkeypatch.setenv('HOME',str(tmp_path));d.install()
    env=dict(os.environ);env.pop('XDG_CONFIG_HOME',None)
    listed=subprocess.run([binary,'skills','list'],env=env,capture_output=True,text=True,check=True)
    shown=subprocess.run([binary,'skills','show','token-optimizer-output'],env=env,capture_output=True,text=True,check=True)
    assert '/token-optimizer-output [user]' in listed.stdout
    assert 'Triggers: user' in shown.stdout
    assert 'Triggers: user, model' not in shown.stdout
    assert str(d.skill_dir()) in shown.stdout


@pytest.mark.parametrize('value', [None, '', 'false', '0', '/unsafe/devin'])
def test_real_cli_requires_explicit_boolean_opt_in(tmp_path, monkeypatch, value):
    import devin_install as d, shutil, subprocess
    calls = []
    if value is None: monkeypatch.delenv('DEVIN_TEST_BINARY', raising=False)
    else: monkeypatch.setenv('DEVIN_TEST_BINARY', value)
    monkeypatch.setattr(shutil, 'which', lambda name: calls.append(('which', name)) or '/fake/devin')
    monkeypatch.setattr(d, 'install', lambda: calls.append(('install',)))
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: calls.append(('run',)))
    with pytest.raises(pytest.skip.Exception): test_genuine_devin_cli_discovery(tmp_path, monkeypatch)
    assert calls == []

@pytest.mark.parametrize('value', ['1', 'true', 'YES', 'on'])
def test_real_cli_opt_in_uses_only_fixed_binary(tmp_path, monkeypatch, value):
    import devin_install as d, shutil, subprocess
    from types import SimpleNamespace
    calls = []
    monkeypatch.setenv('DEVIN_TEST_BINARY', value)
    monkeypatch.setattr(shutil, 'which', lambda name: calls.append(('which', name)) or '/fake/devin')
    monkeypatch.setattr(d, 'install', lambda: calls.append(('install',)))
    def run(argv, **kwargs):
        calls.append(('run', argv, kwargs['env'].copy()))
        text = '/token-optimizer-output [user]' if argv[-1] == 'list' else 'Triggers: user ' + str(d.skill_dir())
        return SimpleNamespace(stdout=text)
    monkeypatch.setattr(subprocess, 'run', run)
    test_genuine_devin_cli_discovery(tmp_path, monkeypatch)
    assert calls[0] == ('which', 'devin') and calls[1] == ('install',)
    assert [call[1][0] for call in calls[2:]] == ['/fake/devin', '/fake/devin']
    assert all(call[2]['HOME'] == str(tmp_path) and 'XDG_CONFIG_HOME' not in call[2] for call in calls[2:])


@pytest.mark.parametrize('kind', ['XDG_CONFIG_HOME', 'TOKEN_OPTIMIZER_DEVIN_HOME'])
def test_valid_inside_home_override_is_honored(tmp_path, monkeypatch, kind):
    import devin_state as state
    import devin_install as install
    override = tmp_path / 'custom-config'
    monkeypatch.setenv(kind, str(override))
    expected = override / 'devin' if kind == 'XDG_CONFIG_HOME' else override
    assert state.home() == expected
    install.install()
    assert (expected / 'skills' / 'token-optimizer-output' / 'SKILL.md').is_file()
    assert (expected / 'token-optimizer' / 'consent.json').is_file()
    capture = state.prepare_capture()
    assert capture.is_relative_to(expected / 'token-optimizer')
    assert not (tmp_path / '.config' / 'devin').exists()
    install.uninstall()
    assert not (expected / 'skills' / 'token-optimizer-output').exists()


def test_loader_path_default_and_override_are_distinct(tmp_path, monkeypatch):
    import devin_state as s
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('XDG_CONFIG_HOME', raising=False)
    monkeypatch.delenv('TOKEN_OPTIMIZER_DEVIN_HOME', raising=False)
    assert s.loader_path_info()['status'] == 'documented_default'
    monkeypatch.setenv('TOKEN_OPTIMIZER_DEVIN_HOME', str(tmp_path / 'alternate'))
    assert s.loader_path_info()['status'] == 'nondefault_discovery_unknown'
    assert s.loader_path_info()['desktop_loading_verified'] is False


def test_nondefault_install_warns_without_changing_loader_config(tmp_path, monkeypatch, capsys):
    import devin_install as d
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('TOKEN_OPTIMIZER_DEVIN_HOME', str(tmp_path / 'alternate'))
    d.install()
    assert 'Desktop discovery is unverified' in capsys.readouterr().out
    assert not (tmp_path / '.config/devin/config.json').exists()


def test_desktop_bundle_presence_without_cli_does_not_claim_live_proof(tmp_path, monkeypatch):
    import devin_doctor as d
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr(d.shutil, 'which', lambda _: None)
    app = tmp_path / '.local/share/Devin/resources/app'
    app.mkdir(parents=True)
    (app / 'product.json').write_text('{"applicationName":"devin-desktop"}')
    agent = app / 'extensions/windsurf/devin/bin/devin'
    agent.parent.mkdir(parents=True)
    agent.write_text('not executed')
    facts = d.local_agent_surfaces()
    assert facts['cli_on_path'] is False
    assert facts['desktop_bundle_present'] is True
    assert facts['bundled_agent_file_found'] is True
    assert facts['desktop_loading_verified'] is False
    assert facts['desktop_execution_verified'] is False


def test_absent_desktop_and_cli_are_separate_facts(tmp_path, monkeypatch):
    import devin_doctor as d
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr(d.shutil, 'which', lambda _: None)
    monkeypatch.setattr(d, '_desktop_app_roots', lambda: [tmp_path / 'absent'])
    facts = d.local_agent_surfaces()
    assert facts['desktop_bundle_present'] is False
    assert facts['bundled_agent_file_found'] is False
    assert facts['cli_on_path'] is False


# Explicit ids: pytest derives the id from the parameter value, and a megabyte
# id overflows the Windows environment limit when pytest exports the test name.
@pytest.mark.parametrize('content',[b'\xff',b'[]',b'{',b'{"applicationName":"windsurf"}',b'x' * (1024*1024+1)],
                         ids=['not-utf8','list','truncated','other-app','oversize'])
def test_desktop_metadata_unusable_is_not_presence(tmp_path, monkeypatch, content):
    import devin_doctor as d
    root=tmp_path/'app';root.mkdir();(root/'product.json').write_bytes(content)
    monkeypatch.setattr(d,'_desktop_app_roots',lambda:[root])
    result=d.local_agent_surfaces()
    assert not result['desktop_bundle_present']
    assert not result['desktop_execution_verified']


def test_desktop_metadata_fifo_is_nonblocking(tmp_path):
    import subprocess
    root=tmp_path/'app';root.mkdir();os.mkfifo(root/'product.json')
    code=('import sys;sys.path.insert(0,'+repr(str(SCRIPTS))+');'
          'import devin_doctor as d;from pathlib import Path;'
          'd._desktop_app_roots=lambda:[Path('+repr(str(root))+')];'
          'assert not d.local_agent_surfaces()["desktop_bundle_present"]')
    subprocess.run([sys.executable,'-c',code],check=True,timeout=5)


def test_desktop_directory_metadata_closes_fd(tmp_path, monkeypatch):
    import devin_doctor as d
    root=tmp_path/'app';root.mkdir();(root/'product.json').mkdir()
    monkeypatch.setattr(d,'_desktop_app_roots',lambda:[root])
    original_open=d.os.open
    original_close=d.os.close
    opened=[];closed=[]
    def record_open(*args,**kwargs):
        fd=original_open(*args,**kwargs);opened.append(fd);return fd
    def record_close(fd):
        closed.append(fd);return original_close(fd)
    monkeypatch.setattr(d.os,'open',record_open)
    monkeypatch.setattr(d.os,'close',record_close)
    for _ in range(25):
        assert not d.local_agent_surfaces()['desktop_bundle_present']
    assert opened and opened==closed


@pytest.mark.parametrize('override',[False,True])
def test_desktop_diagnose_real_install(tmp_path, monkeypatch, override, capsys):
    import devin_install as install
    import devin_doctor as doctor
    monkeypatch.setenv('HOME',str(tmp_path))
    monkeypatch.delenv('XDG_CONFIG_HOME',raising=False)
    monkeypatch.delenv('TOKEN_OPTIMIZER_DEVIN_HOME',raising=False)
    if override:
        monkeypatch.setenv('TOKEN_OPTIMIZER_DEVIN_HOME',str(tmp_path/'custom/devin'))
    install.install(dry_run=True)
    preview=capsys.readouterr().out
    assert ('Desktop discovery is unverified' in preview)==override
    assert not (tmp_path/'.config/devin/token-optimizer/install.json').exists()
    install.install()
    actual=capsys.readouterr().out
    assert ('Desktop discovery is unverified' in actual)==override
    result=doctor.diagnose(probe=True)
    assert result['installed'] and result['local_probe_passed']
    assert result['loader_path']['status']==('nondefault_discovery_unknown' if override else 'documented_default')
    assert any('Nondefault skill directory' in w for w in result['warnings'])==override
    assert result['devin_cli_on_path']==result['local_agent_surfaces']['cli_on_path']
    assert not result['local_agent_surfaces']['desktop_loading_verified']
    assert not result['local_agent_surfaces']['desktop_execution_verified']
    install.uninstall()


def test_desktop_json_depth_is_unusable(tmp_path, monkeypatch):
    import devin_doctor as doctor
    root = tmp_path / 'bundle'
    root.mkdir()
    (root / 'product.json').write_text('{}')
    monkeypatch.setattr(doctor, '_desktop_app_roots', lambda: [root])
    def recursion_failure(*args, **kwargs):
        raise RecursionError('injected parser recursion failure')
    monkeypatch.setattr(doctor.json, 'loads', recursion_failure)
    assert not doctor.local_agent_surfaces()['desktop_bundle_present']


def test_symlinked_home_is_not_a_loader_default(tmp_path, monkeypatch):
    import devin_state as state
    real=tmp_path/'real';real.mkdir();link=tmp_path/'home';link.symlink_to(real,target_is_directory=True)
    monkeypatch.setenv('HOME',str(link))
    monkeypatch.delenv('XDG_CONFIG_HOME',raising=False)
    monkeypatch.delenv('TOKEN_OPTIMIZER_DEVIN_HOME',raising=False)
    with pytest.raises(OSError,match='symlink path refused'):
        state.loader_path_info()


@pytest.mark.parametrize('dry',[True,False])
def test_desktop_dry_run_refuses_loose_state(tmp_path, monkeypatch, dry):
    import devin_install as d
    monkeypatch.setenv('HOME',str(tmp_path))
    root=tmp_path/'.config/devin/token-optimizer';root.mkdir(parents=True);root.chmod(0o755)
    with pytest.raises(OSError,match='owner-only'):
        d.install(dry_run=dry)


def test_desktop_doctor_unresolvable_home_reports_error(monkeypatch, capsys):
    import devin_doctor as d
    monkeypatch.setattr(d.state,'home',lambda: (_ for _ in ()).throw(RuntimeError('Cannot determine home directory')))
    assert d.main([])==2
    assert 'Cannot determine home' in capsys.readouterr().err


def test_desktop_metadata_symlink_is_ignored(tmp_path, monkeypatch):
    import devin_doctor as d
    root=tmp_path/'app';root.mkdir();target=tmp_path/'target';target.write_text('{"applicationName":"devin-desktop"}')
    (root/'product.json').symlink_to(target)
    monkeypatch.setattr(d,'_desktop_app_roots',lambda:[root])
    assert not d.local_agent_surfaces()['desktop_bundle_present']


@pytest.mark.parametrize('dry',[True,False])
def test_desktop_preflight_refuses_foreign_skill(tmp_path, monkeypatch, dry):
    import devin_install as d
    monkeypatch.setenv('HOME',str(tmp_path))
    skill=tmp_path/'.config/devin/skills/token-optimizer-output';skill.mkdir(parents=True)
    (skill/'SKILL.md').write_text('foreign')
    with pytest.raises(OSError,match='foreign install'):
        d.install(dry_run=dry)
    assert (skill/'SKILL.md').read_text()=='foreign'


def test_desktop_dry_pending_does_not_claim_success(tmp_path, monkeypatch):
    import devin_install as d
    monkeypatch.setenv('HOME',str(tmp_path))
    root=tmp_path/'.config/devin/token-optimizer';root.mkdir(parents=True);root.chmod(0o700)
    pending=root/'pending-install.json';pending.write_text('{}');pending.chmod(0o600)
    with pytest.raises(OSError,match='cannot predict'):
        d.install(dry_run=True)
    assert pending.read_text()=='{}'


@pytest.mark.parametrize('dry',[True,False])
def test_desktop_preflight_refuses_unsafe_consent(tmp_path, monkeypatch, dry):
    import devin_install as d
    monkeypatch.setenv('HOME',str(tmp_path))
    root=tmp_path/'.config/devin/token-optimizer';root.mkdir(parents=True);root.chmod(0o700)
    consent=root/'consent.json';consent.write_text('{}');consent.chmod(0o644)
    with pytest.raises(OSError,match='owner-only'):
        d.install(dry_run=dry)


@pytest.mark.parametrize('dry',[True,False])
def test_desktop_preflight_refuses_dangling_pending(tmp_path, monkeypatch, dry):
    import devin_install as d
    monkeypatch.setenv('HOME',str(tmp_path))
    root=tmp_path/'.config/devin/token-optimizer';root.mkdir(parents=True);root.chmod(0o700)
    (root/'pending-install.json').symlink_to(root/'missing')
    with pytest.raises(OSError,match='symlink'):
        d.install(dry_run=dry)


@pytest.mark.parametrize('leaf',['install.lock','consent.json','pending-install.json'])
@pytest.mark.parametrize('dry',[True,False])
def test_desktop_preflight_preserves_special_state(tmp_path, monkeypatch, leaf, dry):
    import devin_install as d
    monkeypatch.setenv('HOME',str(tmp_path))
    root=tmp_path/'.config/devin/token-optimizer';root.mkdir(parents=True);root.chmod(0o700)
    target=root/leaf;os.mkfifo(target,0o600)
    with pytest.raises(OSError,match='regular'):
        d.install(dry_run=dry)
    import stat
    assert stat.S_ISFIFO(target.lstat().st_mode)


@pytest.mark.parametrize('part',['root','agent'])
def test_desktop_symlink_presence_is_not_reported(tmp_path, monkeypatch, part):
    import devin_doctor as d
    root=tmp_path/'app';root.mkdir();(root/'product.json').write_text('{"applicationName":"devin-desktop"}')
    agent=root/'extensions/windsurf/devin/bin/devin';agent.parent.mkdir(parents=True)
    if part=='agent':
        target=tmp_path/'agent';target.write_text('not executed');agent.symlink_to(target)
    else:
        agent.write_text('not executed');link=tmp_path/'linked';link.symlink_to(root,target_is_directory=True);root=link
    monkeypatch.setattr(d,'_desktop_app_roots',lambda:[root])
    result=d.local_agent_surfaces()
    assert not result['bundled_agent_file_found']
    if part=='root':assert not result['desktop_bundle_present']


@pytest.mark.parametrize('dry',[True,False])
def test_desktop_preflight_refuses_file_parent(tmp_path, monkeypatch, dry):
    import devin_install as d
    monkeypatch.setenv('HOME',str(tmp_path))
    base=tmp_path/'.config/devin';base.mkdir(parents=True);(base/'skills').write_text('foreign')
    with pytest.raises(OSError):d.install(dry_run=dry)
    assert (base/'skills').read_text()=='foreign'


@pytest.mark.parametrize('leaf',['metrics.db','session-salt'])
@pytest.mark.parametrize('dry',[True,False])
def test_desktop_numeric_special_state_warns_without_blocking(tmp_path, monkeypatch, capsys, leaf, dry):
    # Numeric estimates are optional: an unusable metrics leaf must not stop
    # the skill from installing, and must be named with its reason.
    import devin_install as d
    monkeypatch.setenv('HOME',str(tmp_path))
    root=tmp_path/'.config/devin/token-optimizer';root.mkdir(parents=True);root.chmod(0o700)
    os.mkfifo(root/leaf,0o600)
    d.install(dry_run=dry)
    text=capsys.readouterr().out
    assert 'numeric estimates will not be recorded' in text
    assert 'regular state file required: '+str(root/leaf) in text
    assert stat.S_ISFIFO((root/leaf).lstat().st_mode)
    assert (root/'install.json').exists() is (not dry)


def test_desktop_file_parent_refusal_creates_no_state(tmp_path, monkeypatch):
    import devin_install as d
    monkeypatch.setenv('HOME',str(tmp_path))
    base=tmp_path/'.config/devin';base.mkdir(parents=True);(base/'skills').write_text('foreign')
    with pytest.raises(OSError):d.install()
    assert not (base/'token-optimizer').exists()


def test_desktop_state_vanishing_before_lock_is_missing(tmp_path, monkeypatch):
    import devin_install as d
    root=tmp_path/'root';root.mkdir();pending=root/'pending-install.json';pending.write_text('{}');pending.chmod(0o600)
    original=Path.lstat
    def vanish(path,*args,**kwargs):
        if path==pending:
            pending.unlink(missing_ok=True)
            import errno
            raise FileNotFoundError(errno.ENOENT,'vanished',str(path))
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'lstat',vanish)
    d._preflight_state_files(root)


def test_clean_uninstall_is_no_write(tmp_path, monkeypatch):
    import devin_install as d
    monkeypatch.setenv('HOME',str(tmp_path));d.uninstall()
    assert not (tmp_path/'.config/devin').exists()


def test_uninstall_dry_pending_refuses(tmp_path, monkeypatch):
    import devin_install as d
    monkeypatch.setenv('HOME',str(tmp_path))
    root=d.state.data_dir();root.mkdir(parents=True);root.chmod(0o700)
    pending=root/'pending-install.json';pending.write_text('{}');pending.chmod(0o600)
    with pytest.raises(OSError,match='pending'):d.uninstall(dry_run=True)


def test_desktop_doctor_surfaces_pending_recovery(tmp_path, monkeypatch):
    import devin_install as d
    import devin_doctor as doctor
    monkeypatch.setenv('HOME',str(tmp_path));d.install()
    pending=d.state.data_dir()/'pending-install.json';pending.write_text('{}');pending.chmod(0o600)
    result=doctor.diagnose()
    assert result['recovery_pending']
    assert any('Recovery is pending' in w for w in result['warnings'])


def test_probe_write_failure_removes_its_capture(tmp_path, monkeypatch):
    import devin_install as d
    import devin_doctor as doctor
    monkeypatch.setenv('HOME',str(tmp_path));d.install()
    original=Path.write_bytes
    def fail(path,data):
        if path.name=='stdout':raise OSError('fixture write failure')
        return original(path,data)
    monkeypatch.setattr(Path,'write_bytes',fail)
    with pytest.raises(OSError):doctor.diagnose(probe=True)
    assert not list((d.state.data_dir()/'captures').iterdir())


@pytest.mark.parametrize('dry',[True,False])
def test_desktop_purge_directory_state_refuses_before_payload_removal(tmp_path,monkeypatch,dry):
    import devin_install as d
    monkeypatch.setenv('HOME',str(tmp_path));d.install()
    root=d.state.data_dir();leaf=root/'metrics.db-wal';leaf.mkdir(mode=0o700)
    before=(root/'plugin/devin_state.py').read_bytes()
    with pytest.raises(OSError,match='directory'):
        d.uninstall(purge=True,dry_run=dry)
    assert d.manifest_path().exists()
    assert (root/'plugin/devin_state.py').read_bytes()==before and leaf.is_dir()


def test_desktop_purge_retains_current_capture_removal_contract(tmp_path,monkeypatch):
    import devin_install as d
    import devin_state as state
    monkeypatch.setenv('HOME',str(tmp_path));d.install()
    capture=state.prepare_capture();(capture/'stdout').write_bytes(b'harmless fixture')
    d.uninstall(purge=True,dry_run=True)
    assert capture.exists() and d.manifest_path().exists()
    d.uninstall(purge=True)
    assert not capture.exists() and not d.manifest_path().exists()

@pytest.mark.parametrize('dry', [False, True])
def test_desktop_preflight_refuses_writable_ancestor(tmp_path, monkeypatch, dry):
    import devin_install as d
    monkeypatch.setenv('HOME', str(tmp_path))
    config = tmp_path / '.config'
    config.mkdir(mode=0o777)
    config.chmod(0o777)
    with pytest.raises(OSError, match='writable ancestor'):
        d.install(dry_run=dry)
    assert not (config / 'devin').exists()


@pytest.mark.parametrize('kind', ['file', 'symlink', 'loose-capture', 'leaf-symlink'])
@pytest.mark.parametrize('dry', [False, True])
def test_desktop_purge_capture_preflight_is_nonmutating(tmp_path, monkeypatch, kind, dry):
    import devin_install as d
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    root = d.state.data_dir()
    captures = root / 'captures'
    if kind == 'file':
        captures.write_text('foreign')
    elif kind == 'symlink':
        captures.symlink_to(tmp_path / 'missing')
    else:
        capture = captures / ('capture-' + 'a' * 32)
        capture.mkdir(parents=True, mode=0o700)
        captures.chmod(0o700)
        if kind == 'loose-capture':
            capture.chmod(0o755)
        else:
            (capture / 'stdout').symlink_to(tmp_path / 'missing')
    before = (root / 'install.json').read_bytes()
    with pytest.raises(OSError):
        d.uninstall(purge=True, dry_run=dry)
    assert (root / 'install.json').read_bytes() == before
    assert (root / 'plugin/devin_doctor.py').exists()


@pytest.mark.parametrize('kind', ['garbage', 'directory', 'dangling'])
def test_desktop_doctor_pending_is_not_ready(tmp_path, monkeypatch, kind):
    import devin_install as d
    import devin_doctor as doctor
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    pending = d.state.data_dir() / 'pending-install.json'
    if kind == 'directory':
        pending.mkdir(mode=0o700)
    elif kind == 'dangling':
        pending.symlink_to(tmp_path / 'missing')
    else:
        pending.write_text('garbage')
        pending.chmod(0o600)
    result = doctor.diagnose(probe=True)
    assert result['recovery_pending']
    assert not result['installed']
    assert result['status'] == 'recovery-pending'
    assert result['local_probe_passed'] is None
    assert doctor.main([]) == 1


def test_desktop_loader_normalizes_home_spelling(tmp_path, monkeypatch):
    import devin_state as state
    parent = tmp_path / 'parent'
    parent.mkdir()
    monkeypatch.setenv('HOME', str(parent / '..'))
    assert state.loader_path_info()['status'] == 'documented_default'


def test_desktop_wrong_type_error_precedes_mode(tmp_path):
    import devin_state as state
    leaf = tmp_path / 'metrics.db'
    leaf.mkdir(mode=0o755)
    with pytest.raises(OSError, match='regular state file'):
        state.private_path(leaf)

@pytest.mark.parametrize('leaf', ['metrics.db', 'session-salt', 'metrics.db-journal', 'metrics.db-wal', 'metrics.db-shm'])
@pytest.mark.parametrize('kind', ['loose-mode', 'directory', 'symlink'])
def test_desktop_doctor_reports_unsafe_numeric_state_without_failing(tmp_path, monkeypatch, leaf, kind):
    import devin_install as d
    import devin_doctor as doctor
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    target = d.state.data_dir() / leaf
    if kind == 'directory':
        target.mkdir(mode=0o700)
    elif kind == 'symlink':
        target.symlink_to(tmp_path / 'missing')
    else:
        target.write_bytes(b'private numeric bytes')
        target.chmod(0o644)
    before = target.lstat()
    result = doctor.diagnose(probe=True)
    # The payload verifies and the filter works without numeric estimates, so
    # readiness holds; the unsafe leaf is its own field and a named warning.
    assert result['installed'] and result['readiness_error'] is None
    assert result['numeric_state_ok'] is False
    assert result['local_probe_passed'] is True
    named = [w for w in result['warnings'] if w.startswith('Numeric estimates will not be recorded') and str(target) in w]
    assert named and any(word in named[0] for word in ('owner-only', 'regular state file', 'symlink'))
    after = target.lstat()
    assert (before.st_ino, before.st_mode, before.st_size) == (after.st_ino, after.st_mode, after.st_size)
    if kind == 'loose-mode':
        assert target.read_bytes() == b'private numeric bytes'


def test_desktop_doctor_safe_numeric_state_ready(tmp_path, monkeypatch):
    import devin_install as d
    import devin_doctor as doctor
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    for leaf in ('metrics.db', 'session-salt', 'metrics.db-journal', 'metrics.db-wal', 'metrics.db-shm'):
        target = d.state.data_dir() / leaf
        target.write_bytes(b'private numeric bytes')
        target.chmod(0o600)
    result = doctor.diagnose()
    assert result['installed']
    assert result['readiness_error'] is None

@pytest.mark.parametrize('purge', [False, True])
def test_desktop_uninstall_dry_refuses_loose_root(tmp_path, monkeypatch, purge):
    import devin_install as d
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    root = d.state.data_dir()
    root.chmod(0o755)
    with pytest.raises(OSError, match=str(root)):
        d.uninstall(dry_run=True, purge=purge)
    assert (root / 'install.json').exists()


@pytest.mark.skipif(os.name != 'posix' or os.geteuid() == 0, reason='root bypasses chmod access checks')
@pytest.mark.parametrize('kind', ['unwritable', 'directory-leaf'])
def test_desktop_purge_refuses_unusable_capture(tmp_path, monkeypatch, kind):
    import devin_install as d
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    capture = d.state.prepare_capture()
    if kind == 'unwritable':
        capture.chmod(0o500)
    else:
        (capture / 'stdout').unlink()
        (capture / 'stdout').mkdir(mode=0o700)
    try:
        with pytest.raises(OSError, match='capture'):
            d.uninstall(purge=True)
        assert (d.state.data_dir() / 'install.json').exists()
    finally:
        capture.chmod(0o700)


def test_desktop_purge_fifo_pending_is_nonblocking(tmp_path, monkeypatch):
    import devin_install as d
    import subprocess
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    os.mkfifo(d.state.data_dir() / 'pending-install.json', 0o600)
    result = subprocess.run([sys.executable, '-I', str(SCRIPTS / 'devin_install.py'), 'uninstall', '--purge'], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert not (d.state.data_dir() / 'install.json').exists()


def test_desktop_pending_preserves_tamper_diagnosis(tmp_path, monkeypatch):
    import devin_install as d
    import devin_doctor as doctor
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    root = d.state.data_dir()
    (root / 'plugin/devin_state.py').write_text('tampered')
    (root / 'pending-install.json').write_text('{}')
    (root / 'pending-install.json').chmod(0o600)
    result = doctor.diagnose()
    assert 'Recovery is pending' in result['readiness_error']
    assert result['readiness_error'] != 'Recovery is pending'


@pytest.mark.skipif(os.name != 'posix' or os.geteuid() == 0, reason='root bypasses chmod access checks')
@pytest.mark.parametrize('target,mode', [('skill', 0o500), ('lock', 0o400)])
def test_desktop_install_dry_refuses_unwritable_target(tmp_path, monkeypatch, target, mode):
    import devin_install as d
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    path = d.skill_dir() if target == 'skill' else d.state.data_dir() / 'install.lock'
    path.chmod(mode)
    try:
        with pytest.raises(OSError, match='writable'):
            d.install(dry_run=True)
    finally:
        path.chmod(0o700 if target == 'skill' else 0o600)


def test_desktop_purge_rechecks_after_lock(tmp_path, monkeypatch):
    import devin_install as d
    from contextlib import contextmanager
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    root = d.state.data_dir()
    original = d._lock
    @contextmanager
    def changed_after_lock():
        with original():
            (root / 'captures').write_text('foreign')
            yield
    monkeypatch.setattr(d, '_lock', changed_after_lock)
    with pytest.raises(OSError, match='captures root'):
        d.uninstall(purge=True)
    assert (root / 'install.json').exists()
    assert (root / 'plugin/devin_doctor.py').exists()


def test_desktop_purge_rechecks_state_after_lock(tmp_path, monkeypatch):
    import devin_install as d
    from contextlib import contextmanager
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    root = d.state.data_dir()
    original = d._lock
    @contextmanager
    def changed_after_lock():
        with original():
            (root / 'metrics.db-wal').mkdir(mode=0o700)
            yield
    monkeypatch.setattr(d, '_lock', changed_after_lock)
    with pytest.raises(OSError, match='directory'):
        d.uninstall(purge=True)
    assert (root / 'install.json').exists()
    assert (root / 'plugin/devin_state.py').exists()


def test_desktop_refresh_search_only_skill_parent(tmp_path, monkeypatch):
    import devin_install as d
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    parent = d.skill_dir().parent
    parent.chmod(0o500)
    try:
        d.install(dry_run=True)
        d.install()
    finally:
        parent.chmod(0o700)


@pytest.mark.skipif(os.name != 'posix' or os.geteuid() == 0, reason='root bypasses chmod access checks')
@pytest.mark.parametrize('target,mode', [('lock', 0o200), ('plugin', 0o500), ('skill', 0o500), ('captures', 0o500), ('capture', 0o300)])
@pytest.mark.parametrize('dry', [True, False])
def test_desktop_remove_preflight_access(tmp_path, monkeypatch, target, mode, dry):
    import devin_install as d
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    root = d.state.data_dir()
    capture = d.state.prepare_capture()
    paths = {'root': root, 'lock': root / 'install.lock', 'plugin': root / 'plugin', 'skill': d.skill_dir(), 'captures': root / 'captures', 'capture': capture}
    path = paths[target]
    path.chmod(mode)
    try:
        with pytest.raises(OSError):
            d.uninstall(dry_run=dry, purge=True)
    finally:
        path.chmod(0o600 if target == 'lock' else 0o700)
    assert (root / 'install.json').exists()
    assert (root / 'plugin/devin_state.py').exists()


@pytest.mark.parametrize('kind', ['loose', 'hardlink', 'pending'])
def test_desktop_purge_retains_old_record_digests(tmp_path, monkeypatch, kind):
    import devin_install as d
    import hashlib
    import json
    import subprocess
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    root = d.state.data_dir()
    manifest = root / 'install.json'
    record = json.loads(manifest.read_text())
    for name in ('devin_state.py', 'devin_doctor.py'):
        # Historically distinct bytes, independent of git history or checkout depth.
        old = ('# prior-version synthetic payload fixture: ' + name + '\n').encode()
        target = root / 'plugin' / name
        target.write_bytes(old)
        record['files'][str(target)] = hashlib.sha256(old).hexdigest()
    manifest.write_text(json.dumps(record))
    if kind == 'hardlink':
        os.link(manifest, tmp_path / 'record-backup')
    elif kind == 'pending':
        manifest.rename(root / 'pending-install.json')
        (root / 'pending-install.json').chmod(0o644)
    else:
        manifest.chmod(0o644)
    d.uninstall(purge=True)
    assert not (root / 'plugin/devin_state.py').exists()
    assert not (root / 'plugin/devin_doctor.py').exists()

@pytest.mark.parametrize('mask', [0o020, 0o002])
def test_desktop_ancestor_write_masks(tmp_path, monkeypatch, mask):
    import devin_install as d
    monkeypatch.setenv('HOME', str(tmp_path))
    config = tmp_path / '.config'
    config.mkdir()
    config.chmod(0o700 | mask)
    with pytest.raises(OSError, match='writable ancestor'):
        d.install(dry_run=True)
    assert not (config / 'devin').exists()


@pytest.mark.parametrize('leaf', ['stdout', 'stderr', '.prepared', 'complete.json'])
def test_desktop_purge_all_fixed_leaf_types(tmp_path, monkeypatch, leaf):
    import devin_install as d
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    capture = d.state.prepare_capture()
    target = capture / leaf
    target.unlink(missing_ok=True)
    target.mkdir(mode=0o700)
    with pytest.raises(OSError, match='regular'):
        d.uninstall(purge=True)
    assert (d.state.data_dir() / 'install.json').exists()


def test_desktop_access_uses_effective_identity_and_search(tmp_path, monkeypatch):
    import devin_install as d
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    calls = []
    def access(path, mode, *, effective_ids=False):
        calls.append((Path(path), mode, effective_ids))
        return effective_ids and bool(mode & os.X_OK)
    monkeypatch.setattr(d.os, 'access', access)
    with pytest.raises(OSError, match='lock'):
        d.install(dry_run=True)
    directories = [entry for entry in calls if entry[0].is_dir()]
    assert {d.state.data_dir(), d.state.data_dir() / 'plugin', d.skill_dir()} <= {entry[0] for entry in directories}
    assert all(entry[2] and entry[1] & os.X_OK for entry in directories)


@pytest.mark.parametrize('part', ['plugin', 'skill'])
def test_desktop_dry_refuses_loose_destination(tmp_path, monkeypatch, part):
    import devin_install as d
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    target = d.state.data_dir() / 'plugin' if part == 'plugin' else d.skill_dir()
    target.chmod(0o755)
    with pytest.raises(OSError, match=str(target)):
        d.install(dry_run=True)


def test_desktop_numeric_state_hardlink_warns_and_names_leaf(tmp_path, monkeypatch, capsys):
    import devin_install as d
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    leaf = d.state.data_dir() / 'metrics.db-journal'
    leaf.write_bytes(b'local')
    leaf.chmod(0o600)
    os.link(leaf, tmp_path / 'linked')
    capsys.readouterr()
    d.install(dry_run=True)
    text = capsys.readouterr().out
    assert 'numeric estimates will not be recorded' in text and str(leaf) in text
    assert leaf.read_bytes() == b'local' and leaf.stat().st_nlink == 2


def test_desktop_install_state_hardlink_refused(tmp_path, monkeypatch):
    # Install-critical state still refuses, and names the leaf.
    import devin_install as d
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    leaf = d.state.data_dir() / 'consent.json'
    os.link(leaf, tmp_path / 'linked')
    for dry in (True, False):
        with pytest.raises(OSError, match=str(leaf)):
            d.install(dry_run=dry)

@pytest.mark.skipif(os.name != 'posix' or os.geteuid() == 0, reason='root bypasses chmod access checks')
@pytest.mark.parametrize('mode', [0o000, 0o500])
@pytest.mark.parametrize('dry', [False, True])
def test_desktop_removal_cache_preflight(tmp_path, monkeypatch, mode, dry):
    import devin_install as d
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    root = d.state.data_dir()
    cache = root / 'plugin/__pycache__'
    cache.mkdir(exist_ok=True)
    (cache / 'test.pyc').write_bytes(b'cache')
    cache.chmod(mode)
    try:
        with pytest.raises(OSError, match='cache'):
            d.uninstall(dry_run=dry)
        assert (root / 'install.json').exists()
        assert (root / 'plugin/devin_state.py').exists()
    finally:
        cache.chmod(0o700)
    d.uninstall()
    d.uninstall(purge=True)
    d.install()


@pytest.mark.skipif(os.name != 'posix' or os.geteuid() == 0, reason='root bypasses chmod access checks')
def test_desktop_removal_rechecks_after_lock(tmp_path, monkeypatch):
    import devin_install as d
    from contextlib import contextmanager
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    root = d.state.data_dir()
    original = d._lock
    @contextmanager
    def changed_after_lock():
        with original():
            root.chmod(0o500)
            yield
    monkeypatch.setattr(d, '_lock', changed_after_lock)
    try:
        with pytest.raises(OSError):
            d.uninstall(purge=True)
    finally:
        root.chmod(0o700)
    assert (root / 'install.json').exists()
    assert (root / 'plugin/devin_state.py').exists()


def test_desktop_purge_reports_extra_capture_content(tmp_path, monkeypatch, capsys):
    import devin_install as d
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    capture = d.state.prepare_capture()
    extra = capture / 'foreign.txt'
    extra.write_text('preserve')
    d.uninstall(purge=True)
    output = capsys.readouterr().out
    assert extra.read_text() == 'preserve'
    assert str(capture.parent) in output
    assert 'capture content was preserved' in output


def test_desktop_fresh_install_write_search_ancestor(tmp_path, monkeypatch):
    import devin_install as d
    monkeypatch.setenv('HOME', str(tmp_path))
    home = d.state.home()
    home.mkdir(parents=True, mode=0o700)
    home.chmod(0o300)
    try:
        d.install(dry_run=True)
        d.install()
    finally:
        home.chmod(0o700)


@pytest.mark.skipif(os.name != 'posix' or os.geteuid() == 0, reason='root bypasses chmod access checks')
def test_desktop_purge_unreadable_foreign_folder_reaches_captures(tmp_path, monkeypatch, capsys):
    import devin_install as d
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    root = d.state.data_dir()
    capture = d.state.prepare_capture()
    plugin = root / 'plugin'
    foreign = plugin / 'foreign.txt'
    foreign.write_text('keep')
    plugin.chmod(0o300)
    try:
        d.uninstall(purge=True, dry_run=True)
        d.uninstall(purge=True)
        assert not capture.exists()
        assert foreign.exists()
        assert str(plugin) in capsys.readouterr().out
    finally:
        plugin.chmod(0o700)


def test_desktop_purge_skips_foreign_symlink_plugin(tmp_path, monkeypatch):
    import devin_install as d
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    root = d.state.data_dir()
    capture = d.state.prepare_capture()
    plugin = root / 'plugin'
    plugin.rename(root / 'parked-plugin')
    unrelated = tmp_path / 'unrelated'
    unrelated.mkdir()
    precious = unrelated / 'precious.txt'
    precious.write_text('KEEP')
    (unrelated / '__pycache__').mkdir()
    (unrelated / '__pycache__/precious.pyc').write_text('KEEP CACHE')
    plugin.symlink_to(unrelated, target_is_directory=True)
    d.uninstall(purge=True)
    assert plugin.is_symlink()
    assert precious.read_text() == 'KEEP'
    assert (unrelated / '__pycache__/precious.pyc').read_text() == 'KEEP CACHE'
    assert not capture.exists()
    assert not (root / 'consent.json').exists()


@pytest.mark.skipif(os.name != 'posix' or os.geteuid() == 0, reason='root bypasses chmod access checks')
@pytest.mark.parametrize('part', ['home', 'config', 'devin'])
def test_desktop_purge_preflights_capture_descriptor_ancestors(tmp_path, monkeypatch, part):
    import devin_install as d
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    root = d.state.data_dir()
    capture = d.state.prepare_capture()
    ancestor = {'home': tmp_path, 'config': tmp_path / '.config', 'devin': d.state.home()}[part]
    ancestor.chmod(0o300)
    try:
        with pytest.raises(OSError):
            d.uninstall(purge=True, dry_run=True)
        with pytest.raises(OSError):
            d.uninstall(purge=True)
    finally:
        ancestor.chmod(0o700)
    assert (root / 'install.json').exists()
    assert (capture / 'stdout').exists()

@pytest.mark.skipif(os.name != 'posix' or os.geteuid() == 0, reason='root bypasses chmod access checks')
@pytest.mark.parametrize('contents', ['empty', 'foreign'])
def test_desktop_purge_search_only_ancestor_without_captures(tmp_path, monkeypatch, contents):
    import devin_install as d
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    root = d.state.data_dir()
    captures = root / 'captures'
    captures.mkdir(mode=0o700)
    if contents == 'foreign':
        (captures / 'foreign.txt').write_text('KEEP')
    for leaf in ('metrics.db', 'session-salt'):
        (root / leaf).write_bytes(b'local')
        (root / leaf).chmod(0o600)
    tmp_path.chmod(0o300)
    try:
        d.uninstall(purge=True, dry_run=True)
        d.uninstall(purge=True)
        assert not (root / 'consent.json').exists()
        assert not (root / 'metrics.db').exists()
        assert not (root / 'session-salt').exists()
        if contents == 'foreign':
            assert (captures / 'foreign.txt').read_text() == 'KEEP'
    finally:
        tmp_path.chmod(0o700)

@pytest.mark.skipif(os.name != 'posix' or os.geteuid() == 0, reason='root bypasses chmod access checks')
@pytest.mark.parametrize('dry', [False, True])
def test_desktop_foreign_unwritable_directory_is_not_ours_to_refuse(tmp_path, monkeypatch, capsys, dry):
    # Nothing is owned, so nothing would be removed: a foreign folder's mode is
    # not a reason to refuse, and the message must not tell the user to move it.
    import devin_install as d
    monkeypatch.setenv('HOME', str(tmp_path))
    root = d.state.data_dir()
    plugin = root / 'plugin'
    plugin.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    plugin.chmod(0o000)
    try:
        d.uninstall(dry_run=dry)
        assert 'No owned Devin install found.' in capsys.readouterr().out
        assert plugin.is_dir()
    finally:
        plugin.chmod(0o700)


@pytest.mark.skipif(os.name != 'posix' or os.geteuid() == 0, reason='root bypasses chmod access checks')
def test_desktop_purge_skips_foreign_readonly_skill_and_still_purges_captures(tmp_path, monkeypatch, capsys):
    import devin_install as d
    monkeypatch.setenv('HOME', str(tmp_path))
    foreign = d.skill_dir(); foreign.mkdir(parents=True)
    (foreign / 'NOTES.md').write_text('mine')
    root = d.state.data_dir(); root.mkdir(mode=0o700)
    d.state.enable_consent()
    capture = d.state.prepare_capture()
    (root / 'metrics.db').write_bytes(b'x'); (root / 'metrics.db').chmod(0o600)
    foreign.chmod(0o555)
    try:
        d.uninstall(purge=True)
        text = capsys.readouterr().out
        assert 'were preserved under ' + str(foreign) in text
        assert 'No owned Devin skill/payload was installed.' in text
        assert not capture.exists() and not (root / 'metrics.db').exists()
        assert (foreign / 'NOTES.md').read_text() == 'mine'
    finally:
        foreign.chmod(0o755)


@pytest.mark.skipif(os.name != 'posix' or os.geteuid() == 0, reason='root bypasses chmod access checks')
def test_desktop_readonly_skills_parent_refuses_before_removal_then_recovers(tmp_path, monkeypatch, capsys):
    import devin_install as d
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    parent = d.skill_dir().parent
    before = sorted(str(p) for p in (tmp_path / '.config/devin').rglob('*'))
    parent.chmod(0o555)
    try:
        for dry in (True, False):
            with pytest.raises(OSError, match=str(parent) + '; make it writable'):
                d.uninstall(dry_run=dry)
        assert sorted(str(p) for p in (tmp_path / '.config/devin').rglob('*')) == before
    finally:
        parent.chmod(0o755)
    d.uninstall()
    assert not d.skill_dir().exists()
    d.install()
    assert (d.skill_dir() / 'SKILL.md').is_file()


def test_desktop_empty_leftover_skill_dir_is_not_foreign(tmp_path, monkeypatch):
    import devin_install as d
    monkeypatch.setenv('HOME', str(tmp_path))
    d.skill_dir().mkdir(parents=True, mode=0o700)
    d.install(dry_run=True)
    d.install()
    assert (d.skill_dir() / 'SKILL.md').is_file()


def test_desktop_uninstall_with_only_a_foreign_skill_writes_nothing(tmp_path, monkeypatch, capsys):
    import devin_install as d
    monkeypatch.setenv('HOME', str(tmp_path))
    d.skill_dir().mkdir(parents=True)
    (d.skill_dir() / 'SKILL.md').write_text('mine')
    d.uninstall()
    assert 'No owned Devin install found.' in capsys.readouterr().out
    assert not d.state.data_dir().exists()
    assert (d.skill_dir() / 'SKILL.md').read_text() == 'mine'


def test_desktop_unremovable_owned_file_keeps_record_and_rerun_completes(tmp_path, monkeypatch):
    import devin_install as d
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    stuck = d.state.data_dir() / 'plugin/devin_verify.py'
    original = Path.unlink
    def refuse(path, *args, **kwargs):
        if path == stuck:
            raise PermissionError(1, 'Operation not permitted', str(path))
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', refuse)
    with pytest.raises(OSError, match='could not remove owned files: ' + str(stuck)) as caught:
        d.uninstall()
    assert 'ownership record was kept' in str(caught.value)
    assert stuck.exists() and d.manifest_path().exists()
    assert (d.state.data_dir() / 'consent.json').exists()
    monkeypatch.setattr(Path, 'unlink', original)
    d.uninstall()
    assert not stuck.exists() and not d.manifest_path().exists() and not d.skill_dir().exists()


@pytest.mark.skipif(os.name != 'posix' or os.geteuid() == 0, reason='root bypasses chmod access checks')
def test_desktop_purge_with_inaccessible_foreign_plugin_dir_preserves_it(tmp_path, monkeypatch, capsys):
    # Path.exists() raises on EACCES before Python 3.14; purge must not die on
    # a foreign folder it never inspects.
    import devin_install as d
    monkeypatch.setenv('HOME', str(tmp_path))
    plugin = d.state.data_dir() / 'plugin'
    plugin.mkdir(parents=True, mode=0o700)
    d.state.data_dir().chmod(0o700)
    (plugin / 'mine.txt').write_text('mine')
    plugin.chmod(0o000)
    try:
        d.uninstall(purge=True)
        text = capsys.readouterr().out
        assert 'were preserved under ' + str(plugin) in text
        assert 'No owned Devin skill/payload was installed.' in text
    finally:
        plugin.chmod(0o700)
    assert (plugin / 'mine.txt').read_text() == 'mine'


@pytest.mark.skipif(os.name != 'posix' or os.geteuid() == 0, reason='root bypasses chmod access checks')
@pytest.mark.parametrize('dry', [True, False])
def test_desktop_purge_refuses_before_purging_when_adapter_files_cannot_be_removed(tmp_path, monkeypatch, dry):
    # With no usable ownership record the sweep removes unchanged adapter
    # files. If their folder is read-only, purging the records first would
    # strand them, so the whole purge refuses and nothing changes.
    import devin_install as d
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    root = d.state.data_dir()
    d.manifest_path().chmod(0o644)
    plugin = root / 'plugin'
    before = sorted((str(p), p.lstat().st_mode) for p in (tmp_path / '.config/devin').rglob('*'))
    plugin.chmod(0o555)
    try:
        with pytest.raises(OSError, match=str(plugin) + '; it holds unchanged adapter files') as caught:
            d.uninstall(dry_run=dry, purge=True)
        assert 'chmod u+wx' in str(caught.value)
    finally:
        plugin.chmod(0o700)
    assert sorted((str(p), p.lstat().st_mode) for p in (tmp_path / '.config/devin').rglob('*')) == before


def test_desktop_purge_without_usable_record_says_what_it_removed(tmp_path, monkeypatch, capsys):
    import devin_install as d
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    d.manifest_path().chmod(0o644)
    capsys.readouterr()
    d.uninstall(purge=True)
    text = capsys.readouterr().out
    assert 'Removed owned Devin skill/payload.' in text
    assert 'No owned Devin skill/payload was installed.' not in text
    assert not d.skill_dir().exists() and not (d.state.data_dir() / 'plugin').exists()


def test_desktop_forged_record_for_a_foreign_path_cannot_lift_the_edit_refusal(tmp_path, monkeypatch):
    # A pending record naming a non-payload path must not make a user edit
    # look like adapter bytes.
    import devin_install as d
    import hashlib, json
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    root = d.state.data_dir()
    edited = root / 'plugin/token_estimate.py'
    edited.write_bytes(edited.read_bytes() + b'# my edit\n')
    notes = root / 'plugin/my_notes.txt'
    notes.write_text('mine')
    pending = root / 'pending-install.json'
    pending.write_text(json.dumps({'owner': d.MARKER, 'files': {
        str(notes): hashlib.sha256(edited.read_bytes()).hexdigest(),
        str(tmp_path / 'elsewhere.py'): hashlib.sha256(edited.read_bytes()).hexdigest()}}))
    pending.chmod(0o600)
    with pytest.raises(OSError, match='installed file changed; refusing overwrite/removal: ' + str(edited)):
        d.uninstall(purge=True)
    assert edited.read_bytes().endswith(b'# my edit\n') and notes.read_text() == 'mine'


def test_desktop_finder_metadata_is_not_foreign_content(tmp_path, monkeypatch):
    import devin_install as d
    monkeypatch.setenv('HOME', str(tmp_path))
    d.skill_dir().mkdir(parents=True, mode=0o700)
    (d.skill_dir() / '.DS_Store').write_bytes(b'\x00')
    d.install()
    d.install()
    d.uninstall()
    assert (d.skill_dir() / '.DS_Store').exists()
    d.install()
    assert (d.skill_dir() / 'SKILL.md').is_file()


@pytest.mark.skipif(sys.platform != 'darwin' or os.geteuid() == 0, reason='user immutable flag semantics are macOS-specific here')
def test_desktop_immutable_owned_file_refuses_every_lifecycle_command_before_changes(tmp_path, monkeypatch):
    import devin_install as d
    monkeypatch.setenv('HOME', str(tmp_path))
    d.install()
    stuck = d.state.data_dir() / 'plugin/devin_verify.py'
    snapshot = lambda: sorted((str(p), p.lstat().st_mode, p.lstat().st_size) for p in (tmp_path / '.config/devin').rglob('*'))
    os.chflags(stuck, stat.UF_IMMUTABLE)
    try:
        before = snapshot()
        for call in (lambda: d.install(dry_run=True), d.install,
                     lambda: d.uninstall(dry_run=True), d.uninstall,
                     lambda: d.uninstall(dry_run=True, purge=True), lambda: d.uninstall(purge=True)):
            with pytest.raises(OSError, match='immutable and cannot be replaced or removed: ' + str(stuck)):
                call()
            assert snapshot() == before
    finally:
        os.chflags(stuck, 0)
    d.uninstall(purge=True)
    assert not stuck.exists() and not d.skill_dir().exists()
