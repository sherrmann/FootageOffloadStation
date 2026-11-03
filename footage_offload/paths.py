"""File system locations.

Every path can be moved under a test root with the environment variable
FOS_ROOT, so the test suite never touches the real system.
"""
import os

from . import APP_ID

_ROOT = os.environ.get("FOS_ROOT", "")


def _p(path):
    return _ROOT + path if _ROOT else path


CONFIG_DIR = _p(f"/etc/{APP_ID}")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
STATE_DIR = _p(f"/var/lib/{APP_ID}")
STATE_DB = os.path.join(STATE_DIR, "state.db")
SECRET_KEY_FILE = os.path.join(STATE_DIR, "secret_key")
RUN_DIR = _p(f"/run/{APP_ID}")
HELPER_SOCKET = os.path.join(RUN_DIR, "helper.sock")
STICK_MOUNT = _p(f"/media/{APP_ID}-stick")
PROBE_MOUNT = os.path.join(RUN_DIR, "probe")
SHARE_DIR = os.environ.get(
    "FOS_SHARE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"),
)
I18N_DIR = os.path.join(SHARE_DIR, "i18n")
WEB_DIR = os.path.join(SHARE_DIR, "web")

MARKER_NAME = ".footage-offload-station"
