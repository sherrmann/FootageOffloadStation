"""Optional onward copy of a finished run folder to a NAS (rsync or SMB).

Runs in a background thread after the run, at low CPU and I/O priority.
This module only ever copies: nothing is deleted on the stick, the camera
or the NAS, and `rsync --delete` is never used.
"""
import logging
import os
import re
import subprocess
import tempfile
import threading

log = logging.getLogger(__name__)

NAS_TIMEOUT_S = 6 * 3600           # whole copy
RSYNC_IO_TIMEOUT_S = 120           # rsync gives up when no data moves for this long
LOW_PRIORITY = ["nice", "-n", "10", "ionice", "-c3"]
RSYNC_CHMOD = "D755,F644"

_SAFE_SEGMENT = re.compile(r"^[^\x00-\x1f\"';\\*?]+$")


class NasError(Exception):
    pass


def validate_target(method, target):
    """Raise NasError unless `target` is acceptable for `method`."""
    if not target or target.startswith("-") or any(c in target for c in "\n\r\x00"):
        raise NasError("invalid target")
    if method == "rsync":
        if not (target.startswith("rsync://") or re.match(r"^[^/\s]+:", target)):
            raise NasError("rsync target must be host:/path or rsync://host/module/path")
    elif method == "smb":
        parse_smb_target(target)
    else:
        raise NasError("unknown method")


def parse_smb_target(target):
    """"//server/share/sub/path" -> ("//server/share", ["sub", "path"])."""
    m = re.match(r"^//([^/\s]+)/([^/\s]+)((?:/[^/]+)*)/?$", target)
    if not m:
        raise NasError("SMB target must be //server/share[/path]")
    parts = [p for p in m.group(3).split("/") if p]
    for p in parts + [m.group(1), m.group(2)]:
        if p in (".", "..") or not _SAFE_SEGMENT.match(p):
            raise NasError("invalid character in SMB target")
    return f"//{m.group(1)}/{m.group(2)}", parts


def _last_line(text):
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1][:200] if lines else ""


class NasCopy:
    def __init__(self, config, status, state, stick_manager, run=subprocess.run):
        self.config = config
        self.status = status
        self.state = state
        self.stick_manager = stick_manager
        self._run = run
        self._lock = threading.Lock()      # one copy at a time
        self._thread = None

    def after_run(self, info, result):
        if not self.config.get("nas_enabled"):
            return
        if not result.run_folder or not result.files:
            return
        self._thread = threading.Thread(target=self._copy_guarded, args=(result,),
                                        name="nas-copy", daemon=True)
        self._thread.start()

    def join(self, timeout=None):
        t = self._thread
        if t:
            t.join(timeout)

    # internals ---------------------------------------------------------------
    def _set(self, state, detail, run):
        self.status.update(nas={"state": state, "detail": detail, "run": run})

    def _copy_guarded(self, result):
        run_name = result.run_folder
        with self._lock:
            self._set("running", "", run_name)
            try:
                stick = self.stick_manager.stick()
                if stick is None:
                    raise NasError("stick not available")
                src = os.path.join(stick.root, run_name)
                if not os.path.isdir(src):
                    raise NasError("run folder missing")
                method = self.config.get("nas_method")
                target = self.config.get("nas_target")
                validate_target(method, target)
                if method == "rsync":
                    self._rsync(src, run_name, target)
                else:
                    self._smb(src, run_name, target)
            except Exception as e:  # noqa: BLE001
                detail = str(e)[:200]
                log.warning("NAS copy of %s failed: %s", run_name, detail)
                self._set("error", detail, run_name)
                self._record(result.run_id, "error")
                self.status.add_warning("warning.nas_failed", detail=detail)
            else:
                log.info("NAS copy of %s done", run_name)
                self._set("done", "", run_name)
                self._record(result.run_id, "ok")
                self.status.clear_warning("warning.nas_failed")

    def _record(self, run_id, value):
        if run_id is None:
            return
        try:
            self.state.set_nas_state(run_id, value)
        except Exception:  # noqa: BLE001
            log.exception("cannot record NAS state")

    def _exec(self, argv, env=None):
        try:
            p = self._run(LOW_PRIORITY + argv, capture_output=True, text=True,
                          timeout=NAS_TIMEOUT_S, env=env)
        except subprocess.TimeoutExpired:
            raise NasError("timeout")
        except OSError as e:
            raise NasError(f"cannot run {argv[0]}: {e}")
        return p

    def _rsync(self, src, run_name, target):
        user = self.config.get("nas_username")
        password = self.config.get("nas_password")
        env = dict(os.environ)
        argv = ["rsync", "-rt", "--partial", f"--chmod={RSYNC_CHMOD}",
                f"--timeout={RSYNC_IO_TIMEOUT_S}"]
        if target.startswith("rsync://"):
            rest = target[len("rsync://"):]
            if user and "@" not in rest.split("/", 1)[0]:
                rest = f"{user}@{rest}"
            dest = "rsync://" + rest
            if password:
                env["RSYNC_PASSWORD"] = password
        else:
            dest = target
            if user and "@" not in target.split(":", 1)[0]:
                dest = f"{user}@{target}"
            argv += ["-e", "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "
                           "-o ConnectTimeout=20"]
        dest = dest.rstrip("/") + "/" + run_name + "/"
        argv += ["--", src.rstrip("/") + "/", dest]
        p = self._exec(argv, env=env)
        if p.returncode != 0:
            raise NasError(_last_line(p.stderr) or f"rsync exit {p.returncode}")

    def _smb(self, src, run_name, target):
        share, parts = parse_smb_target(target)
        user = self.config.get("nas_username")
        password = self.config.get("nas_password")
        if "\n" in user or "\n" in password:
            raise NasError("invalid credentials")
        cmds = []
        for part in parts + [run_name]:
            cmds += [f'mkdir "{part}"', f'cd "{part}"']
        cmds += ["prompt OFF", "recurse ON", f'lcd "{src}"', "mput *"]
        fd, auth = tempfile.mkstemp(prefix=".nas-auth-")
        try:
            os.chmod(auth, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(f"username = {user}\npassword = {password}\n")
            p = self._exec(["smbclient", share, "-A", auth, "-c", "; ".join(cmds)])
        finally:
            try:
                os.unlink(auth)
            except OSError:
                pass
        # "mkdir" on an existing folder is not an error for us.
        problems = [ln for ln in ((p.stdout or "") + (p.stderr or "")).splitlines()
                    if "NT_STATUS" in ln and "COLLISION" not in ln]
        if p.returncode != 0 or problems:
            raise NasError(_last_line("\n".join(problems)) or _last_line(p.stderr)
                           or f"smbclient exit {p.returncode}")
