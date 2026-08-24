"""institutions.json — curated org list: size, schema, value domains, regex validity."""

import json
import re
from pathlib import Path

from scripts import classify_sources
from scripts.classify_sources import load_institutions

REQUIRED_KEYS = {"name", "match", "subject", "standing", "stance"}
ALLOWED_SUBJECTS = {
    "economics", "foreign_policy", "security", "science_tech",
    "health", "development", "general",
}
ALLOWED_STANDING = {"established", "unknown"}
ALLOWED_STANCE = {"research", "advocacy"}


def _raw_entries():
    """Read the raw JSON via the same script-relative path the loader uses,
    so exact-key checks are unaffected by the loader's added `_compiled` key."""
    path = Path(classify_sources.__file__).resolve().parent.parent / "data" / "institutions.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_loader_returns_enough_entries():
    assert len(load_institutions()) >= 120


def test_loaded_entries_have_required_keys():
    # Loader may add internal keys (`_compiled`); required 5 must be a subset.
    for entry in load_institutions():
        assert REQUIRED_KEYS.issubset(entry.keys())


def test_raw_entries_have_exact_schema():
    for entry in _raw_entries():
        assert set(entry.keys()) == REQUIRED_KEYS


def test_value_domains():
    for entry in _raw_entries():
        assert entry["subject"] in ALLOWED_SUBJECTS
        assert entry["standing"] in ALLOWED_STANDING
        assert entry["stance"] in ALLOWED_STANCE


def test_every_match_regex_compiles():
    for entry in _raw_entries():
        for pattern in entry["match"]:
            re.compile(pattern)
