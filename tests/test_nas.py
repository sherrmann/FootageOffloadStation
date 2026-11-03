import os
from types import SimpleNamespace

from footage_offload.nas import NasCopy, NasError, parse_smb_target, validate_target
from footage_offload.status import StatusBus
from footage_offload.transfer import RunResult

import pytest


class FakeState:
    def __init__(self):
        self.set = []

    def set_nas_state(self, run_id, s):
        self.set.append((run_id, s))


class Recorder:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.calls = []
        self.rc, self.stdout, self.stderr = rc, stdout, stderr
        self.auth_content = None
        self.auth_mode = None

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if "-A" in argv:
            path = argv[argv.index("-A") + 1]
            self.auth_content = open(path).read()
            self.auth_mode = os.stat(path).st_mode & 0o777
            self.auth_path = path
        return SimpleNamespace(returncode=self.rc, stdout=self.stdout, stderr=self.stderr)


def make_nas(cfg, tmp_path, rec, **kw):
    cfg.update(nas_enabled=True, **kw)
    root = tmp_path / "stick"
    (root / "2026-11-03_21-47-05").mkdir(parents=True)
    sm = SimpleNamespace(stick=lambda: SimpleNamespace(root=str(root)))
    status, state = StatusBus(min_interval=0), FakeState()
    nas = NasCopy(cfg, status, state, sm, run=rec)
    result = RunResult(run_id=7, run_folder="2026-11-03_21-47-05", files=3)
    return nas, status, state, result, root


def go(nas, result):
    nas.after_run(None, result)
    nas.join(5)


def test_disabled_and_empty(cfg, tmp_path):
    rec = Recorder()
    nas, status, state, result, _ = make_nas(cfg, tmp_path, rec, nas_target="h:/x")
    cfg["nas_enabled"] = False
    go(nas, result)
    cfg["nas_enabled"] = True
    go(nas, RunResult(run_id=1, run_folder="r", files=0))
    assert rec.calls == []


def test_rsync_ssh(cfg, tmp_path):
    rec = Recorder()
    nas, status, state, result, root = make_nas(cfg, tmp_path, rec, nas_target="nas:/vol/footage",
                                             nas_username="bob")
    go(nas, result)
    argv = rec.calls[0][0]
    assert argv[:5] == ["nice", "-n", "10", "ionice", "-c3"]
    assert "rsync" in argv and "--delete" not in argv
    assert "BatchMode=yes" in argv[argv.index("-e") + 1]
    assert argv[-3] == "--"
    assert argv[-2] == f"{root}/2026-11-03_21-47-05/"
    assert argv[-1] == "bob@nas:/vol/footage/2026-11-03_21-47-05/"
    assert state.set == [(7, "ok")]
    assert status.snapshot()["nas"]["state"] == "done"


def test_rsync_daemon_password_in_env(cfg, tmp_path):
    rec = Recorder()
    nas, *_rest, result, _ = make_nas(cfg, tmp_path, rec, nas_target="rsync://h/mod/p",
                                   nas_password="s3cret")
    go(nas, result)
    argv, kw = rec.calls[0]
    assert kw["env"]["RSYNC_PASSWORD"] == "s3cret"
    assert "s3cret" not in " ".join(argv)


def test_rsync_failure(cfg, tmp_path):
    rec = Recorder(rc=23, stderr="boom\nrsync error: some files could not be transferred\n")
    nas, status, state, result, _ = make_nas(cfg, tmp_path, rec, nas_target="nas:/x")
    go(nas, result)
    assert state.set == [(7, "error")]
    w = {w["id"]: w["params"] for w in status.snapshot()["warnings"]}
    assert "some files" in w["warning.nas_failed"]["detail"]
    assert status.snapshot()["nas"]["state"] == "error"


def test_smb_auth_file(cfg, tmp_path):
    rec = Recorder(stdout="putting file a.mp4\n")
    nas, status, state, result, root = make_nas(
        cfg, tmp_path, rec, nas_method="smb", nas_target="//srv/share/sub/dir",
        nas_username="bob", nas_password="pw")
    go(nas, result)
    argv = rec.calls[0][0]
    assert "pw" not in " ".join(argv)
    assert rec.auth_mode == 0o600
    assert "password = pw" in rec.auth_content
    assert not os.path.exists(rec.auth_path)
    assert "//srv/share" in argv
    cmds = argv[argv.index("-c") + 1]
    assert cmds.startswith('mkdir "sub"; cd "sub"; mkdir "dir"; cd "dir"; mkdir "2026-11-03_21-47-05"')
    assert f'lcd "{root}/2026-11-03_21-47-05"' in cmds and cmds.endswith("mput *")
    assert state.set == [(7, "ok")]


def test_smb_status_error_detected(cfg, tmp_path):
    rec = Recorder(stdout="NT_STATUS_OBJECT_NAME_COLLISION\nNT_STATUS_ACCESS_DENIED opening x\n")
    nas, status, state, result, _ = make_nas(cfg, tmp_path, rec, nas_method="smb",
                                          nas_target="//srv/share")
    go(nas, result)
    assert state.set == [(7, "error")]


def test_smb_collision_only_is_ok(cfg, tmp_path):
    rec = Recorder(stdout="NT_STATUS_OBJECT_NAME_COLLISION making remote directory\n")
    nas, status, state, result, _ = make_nas(cfg, tmp_path, rec, nas_method="smb",
                                          nas_target="//srv/share")
    go(nas, result)
    assert state.set == [(7, "ok")]


def test_validation():
    for m, t in (("rsync", "-e evil"), ("rsync", "nocolon"), ("smb", "//srv"),
                 ("smb", '//srv/share/a";rm'), ("smb", "//srv/share/../x"), ("smb", "-x")):
        with pytest.raises(NasError):
            validate_target(m, t)
    assert parse_smb_target("//srv/share/") == ("//srv/share", [])


def test_invalid_target_fails_without_running(cfg, tmp_path):
    rec = Recorder()
    nas, status, state, result, _ = make_nas(cfg, tmp_path, rec, nas_target="-oProxyCommand=x")
    go(nas, result)
    assert rec.calls == [] and state.set == [(7, "error")]
