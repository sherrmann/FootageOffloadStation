import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Keep every path of the package away from the real system.
os.environ.setdefault("FOS_ROOT", tempfile.mkdtemp(prefix="fos-root-"))


class DictConfig(dict):
    """Minimal stand-in for config.Config in engine tests."""

    def get(self, key):  # noqa: D401 - same signature as Config.get
        return self[key]


@pytest.fixture
def cfg():
    from footage_offload.config import DEFAULTS
    c = DictConfig(DEFAULTS)
    c.update(retry_base_delay_s=0.0, min_free_mb=0, chunk_size_kb=64)
    return c


@pytest.fixture
def env(tmp_path, cfg):
    """A simulated camera "cam" with a GoPro layout, a marked stick, state and status."""
    from footage_offload.camera.simulated import SimulatedBackend
    from footage_offload.paths import MARKER_NAME
    from footage_offload.state import State
    from footage_offload.status import StatusBus
    from footage_offload.stick import Stick

    class Env:
        pass

    e = Env()
    e.tmp = tmp_path
    e.cams = tmp_path / "cams"
    e.camdir = e.cams / "cam" / "DCIM" / "100GOPRO"
    e.camdir.mkdir(parents=True)
    e.stickdir = tmp_path / "stick"
    e.stickdir.mkdir()
    (e.stickdir / MARKER_NAME).write_text("")
    e.backend = SimulatedBackend(str(e.cams))
    e.info = e.backend.device("cam")
    e.stick = Stick(str(e.stickdir))
    e.status = StatusBus(min_interval=0)
    e.state = State(str(tmp_path / "state.db"))
    e.cfg = cfg

    def add(name, size, folder=None):
        d = e.camdir if folder is None else (e.cams / "cam" / folder)
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        p.write_bytes(os.urandom(size))
        return p

    e.add = add

    def run(**kw):
        from footage_offload.transfer import TransferRun
        cam = e.backend.open(e.info)
        r = TransferRun(cam, e.info, e.stick, e.cfg, e.status, e.state,
                        sleep=lambda s: None, **kw)
        return r.execute()

    e.run = run
    e.faults = lambda: e.backend.faults.setdefault(e.info.port, __import__(
        "footage_offload.camera.simulated", fromlist=["Faults"]).Faults())
    return e
