"""Message catalogues, plural rules and Swiss number and date formats.

Catalogues live in `paths.I18N_DIR/<lang>.json` and are shared with the
frontend. An entry is either a string with `{name}` placeholders or a plural
object `{"one": ..., "other": ...}` chosen by the parameter `n`.

All four languages use Swiss conventions: `1'234.5`, `03.11.2026`, `21:47`.
"""
import datetime as _dt
import json
import os
import re
import threading

from . import paths
from .config import LANGUAGES

DEFAULT_LANGUAGE = "en"

_lock = threading.Lock()
_cache = {}                 # lang -> catalogue dict
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


# catalogues -------------------------------------------------------------------
def _catalogue_path(lang):
    return os.path.join(paths.I18N_DIR, f"{lang}.json")


def catalogue(lang):
    """The catalogue for `lang` as a dict ({} if missing or broken)."""
    if lang not in LANGUAGES:
        return {}
    with _lock:
        cached = _cache.get(lang)
    if cached is not None:
        return cached
    try:
        with open(_catalogue_path(lang), encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    with _lock:
        _cache[lang] = data
    return data


def reload():
    """Forget cached catalogues (tests, or after an update)."""
    with _lock:
        _cache.clear()


def available():
    """Languages that have a catalogue file."""
    return [lang for lang in LANGUAGES if os.path.isfile(_catalogue_path(lang))]


# negotiation ------------------------------------------------------------------
def negotiate(accept_language_header, setting="auto"):
    """The UI language: the configured one, or the best match of the
    browser's Accept-Language header, or English."""
    if setting in LANGUAGES:
        return setting
    candidates = []
    for pos, part in enumerate((accept_language_header or "").split(",")):
        part = part.strip()
        if not part:
            continue
        tag, _, rest = part.partition(";")
        q = 1.0
        for param in rest.split(";"):
            key, _, value = param.strip().partition("=")
            if key.strip().lower() == "q":
                try:
                    q = float(value)
                except ValueError:
                    q = 0.0
        if q <= 0:
            continue
        primary = tag.strip().lower().replace("_", "-").split("-")[0]
        candidates.append((-q, pos, primary))
    for _, _, primary in sorted(candidates):
        if primary in LANGUAGES:
            return primary
    return DEFAULT_LANGUAGE


# plurals ----------------------------------------------------------------------
def plural_category(lang, n):
    """CLDR category for integer counts: "one" or "other"."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "other"
    if lang == "fr":
        return "one" if n in (0, 1) else "other"
    return "one" if n == 1 else "other"


# translation ------------------------------------------------------------------
def _lookup(lang, message_id):
    for candidate in (lang, DEFAULT_LANGUAGE):
        entry = catalogue(candidate).get(message_id)
        if isinstance(entry, (str, dict)):
            return candidate, entry
    return None, None


def _format_param(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    return format_number(value)


def _fill(text, params):
    def repl(m):
        key = m.group(1)
        return _format_param(params[key]) if key in params else m.group(0)
    return _PLACEHOLDER.sub(repl, text)


def translate(lang, message_id, **params):
    """Translate `message_id`; falls back to English, then to the id."""
    found_lang, entry = _lookup(lang, message_id)
    if entry is None:
        return message_id
    if isinstance(entry, dict):
        category = plural_category(found_lang, params.get("n", 0))
        text = entry.get(category, entry.get("other"))
        if not isinstance(text, str):
            return message_id
        entry = text
    return _fill(entry, params)


# Swiss formats ----------------------------------------------------------------
def format_number(value, decimals=None):
    """1234.5 -> "1'234.5"; integers without decimals."""
    if isinstance(value, bool):
        value = int(value)
    if decimals is not None:
        text = f"{value:,.{decimals}f}"
    elif isinstance(value, int):
        text = f"{value:,d}"
    else:
        text = format(value, ",")
    return text.replace(",", "'")


_UNITS = ("B", "kB", "MB", "GB", "TB", "PB")


def format_bytes(n):
    """Decimal units: 1234567890 -> "1.2 GB"; below 1000 in bytes."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    sign = "-" if n < 0 else ""
    value = float(abs(n))
    if value < 1000:
        return f"{sign}{int(value)} B"
    unit = 0
    while value >= 999.95 and unit < len(_UNITS) - 1:
        value /= 1000.0
        unit += 1
    return f"{sign}{format_number(value, 1)} {_UNITS[unit]}"


def _to_datetime(value):
    if isinstance(value, _dt.datetime):
        return value
    if isinstance(value, _dt.date):
        return _dt.datetime(value.year, value.month, value.day)
    return _dt.datetime.fromtimestamp(float(value))


def format_date(value):
    """UNIX time, date or datetime (local time) -> "03.11.2026"."""
    return _to_datetime(value).strftime("%d.%m.%Y")


def format_time(value):
    """-> "21:47" (24-hour)."""
    return _to_datetime(value).strftime("%H:%M")


def format_datetime(value):
    """-> "03.11.2026 21:47" (24-hour)."""
    return _to_datetime(value).strftime("%d.%m.%Y %H:%M")
