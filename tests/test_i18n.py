import datetime
import json
import time

import pytest

from footage_offload import i18n, paths


@pytest.fixture
def catalogues(tmp_path, monkeypatch):
    d = tmp_path / "i18n"
    d.mkdir()
    cats = {
        "en": {"_meta": {"language": "en"}, "status.waiting": "Waiting for camera",
               "files.count": {"one": "{n} file", "other": "{n} files"},
               "only.en": "English only", "hello": "Hello {name}"},
        "de": {"_meta": {"language": "de"}, "status.waiting": "Wartet auf Kamera",
               "files.count": {"one": "{n} Datei", "other": "{n} Dateien"},
               "hello": "Hallo {name}"},
        "fr": {"_meta": {"language": "fr"}, "files.count": {"one": "{n} fichier", "other": "{n} fichiers"}},
        "it": {"files.count": {"one": "{n} file", "other": "{n} file"}},
    }
    for lang, data in cats.items():
        (d / f"{lang}.json").write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(paths, "I18N_DIR", str(d))
    i18n.reload()
    yield d
    i18n.reload()


def test_translate_and_fallback(catalogues):
    assert i18n.translate("de", "status.waiting") == "Wartet auf Kamera"
    assert i18n.translate("de", "only.en") == "English only"
    assert i18n.translate("fr", "status.waiting") == "Waiting for camera"
    assert i18n.translate("de", "no.such.id") == "no.such.id"
    assert i18n.translate("xx", "status.waiting") == "Waiting for camera"


def test_placeholders(catalogues):
    assert i18n.translate("de", "hello", name="Anna") == "Hallo Anna"
    assert i18n.translate("de", "hello") == "Hallo {name}"      # missing param stays visible


def test_plurals(catalogues):
    assert i18n.translate("en", "files.count", n=1) == "1 file"
    assert i18n.translate("en", "files.count", n=0) == "0 files"
    assert i18n.translate("de", "files.count", n=2) == "2 Dateien"
    assert i18n.translate("de", "files.count", n=1234) == "1'234 Dateien"
    assert i18n.translate("fr", "files.count", n=0) == "0 fichier"
    assert i18n.translate("fr", "files.count", n=1) == "1 fichier"
    assert i18n.translate("fr", "files.count", n=2) == "2 fichiers"


def test_plural_rules():
    for lang in ("en", "de", "it"):
        assert i18n.plural_category(lang, 1) == "one"
        assert i18n.plural_category(lang, 0) == "other"
        assert i18n.plural_category(lang, 2) == "other"
    assert i18n.plural_category("fr", 0) == "one"
    assert i18n.plural_category("fr", 1) == "one"
    assert i18n.plural_category("fr", 2) == "other"


def test_catalogue_and_available(catalogues):
    assert i18n.catalogue("de")["status.waiting"] == "Wartet auf Kamera"
    assert i18n.catalogue("xx") == {}
    assert set(i18n.available()) == {"en", "de", "fr", "it"}


def test_broken_catalogue(tmp_path, monkeypatch):
    (tmp_path / "en.json").write_text("{broken")
    monkeypatch.setattr(paths, "I18N_DIR", str(tmp_path))
    i18n.reload()
    try:
        assert i18n.translate("en", "x.y") == "x.y"
    finally:
        i18n.reload()


@pytest.mark.parametrize("header,setting,expected", [
    ("de-CH,de;q=0.9,en;q=0.8", "auto", "de"),
    ("fr-CH, fr;q=0.9, en;q=0.8", "auto", "fr"),
    ("es-ES,it;q=0.5,en;q=0.7", "auto", "en"),
    ("es-ES,it;q=0.9,en;q=0.7", "auto", "it"),
    ("es", "auto", "en"),
    ("", "auto", "en"),
    (None, "auto", "en"),
    ("de;q=0,fr", "auto", "fr"),
    ("de-CH", "it", "it"),
    ("garbage;;q=x", "auto", "en"),
])
def test_negotiate(header, setting, expected):
    assert i18n.negotiate(header, setting) == expected


def test_format_number():
    assert i18n.format_number(1234.5) == "1'234.5"
    assert i18n.format_number(1234567) == "1'234'567"
    assert i18n.format_number(12) == "12"
    assert i18n.format_number(-9876.25) == "-9'876.25"
    assert i18n.format_number(1234.567, 1) == "1'234.6"


def test_format_bytes():
    assert i18n.format_bytes(0) == "0 B"
    assert i18n.format_bytes(999) == "999 B"
    assert i18n.format_bytes(1500) == "1.5 kB"
    assert i18n.format_bytes(1_200_000_000) == "1.2 GB"
    assert i18n.format_bytes(64_000_000_000) == "64.0 GB"
    assert i18n.format_bytes(999_960) == "1.0 MB"
    assert i18n.format_bytes(2_500_000_000_000) == "2.5 TB"


def test_format_dates():
    dt = datetime.datetime(2026, 11, 3, 21, 47, 5)
    assert i18n.format_date(dt) == "03.11.2026"
    assert i18n.format_datetime(dt) == "03.11.2026 21:47"
    assert i18n.format_time(dt) == "21:47"
    assert i18n.format_date(datetime.date(2026, 1, 9)) == "09.01.2026"
    ts = time.mktime((2026, 11, 3, 9, 5, 0, 0, 0, -1))
    assert i18n.format_datetime(ts) == "03.11.2026 09:05"
