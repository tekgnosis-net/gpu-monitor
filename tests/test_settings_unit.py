"""
Unit tests for reporting.settings — the deep_merge helper in
particular, where the round-2 review caught a subtle bug about
shallow dict copies aliasing DEFAULT_SETTINGS nested dicts.

These tests deliberately avoid pytest-asyncio / aiohttp so they
run at module-load speed with no fixture overhead.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
from reporting import settings as settings_module  # noqa: E402


def test_deep_merge_does_not_alias_base_nested_dicts():
    """SECURITY/CORRECTNESS: mutating the merged result must not
    mutate the base. An earlier implementation used `dict(base)`
    which is a shallow copy — nested dicts were shared with base,
    and because base is often the module-level DEFAULT_SETTINGS,
    a caller who mutated a returned subsection would silently
    mutate the process-global defaults."""
    base = {
        "collection": {"interval_seconds": 4, "flush_interval_seconds": 60},
        "alerts": {"temperature_c": 80},
    }
    # Capture a deep snapshot of base BEFORE the merge
    base_snapshot = copy.deepcopy(base)

    merged = settings_module.deep_merge(base, {"power": {"rate_per_kwh": 0.15}})

    # Mutate the merged result's nested dict. This should NOT
    # affect base.
    merged["collection"]["interval_seconds"] = 9999
    merged["alerts"]["temperature_c"] = 9999
    merged["alerts"]["new_key"] = "injected"

    # base must be byte-identical to its pre-merge snapshot
    assert base == base_snapshot, (
        "deep_merge aliased a nested dict from base into its "
        "return value — mutating the return changed the base"
    )


def test_deep_merge_does_not_alias_default_settings():
    """Concrete regression: the actual DEFAULT_SETTINGS module
    global must be untouched even after 100 merges and 100
    mutations of the returned dicts."""
    original_snapshot = copy.deepcopy(settings_module.DEFAULT_SETTINGS)

    for i in range(100):
        merged = settings_module.deep_merge(
            settings_module.DEFAULT_SETTINGS,
            {"power": {"rate_per_kwh": 0.10 + i * 0.01}},
        )
        merged["collection"]["interval_seconds"] = 999
        merged["alerts"]["temperature_c"] = 999
        merged["smtp"]["host"] = "evil.example.com"

    assert settings_module.DEFAULT_SETTINGS == original_snapshot, (
        "DEFAULT_SETTINGS was mutated during merge loop"
    )


def test_deep_merge_override_values_are_independent():
    """A caller who mutates the override dict AFTER the merge
    should not see their change reflected in the merged result."""
    base = {"a": 1}
    override = {"b": {"nested": "original"}}

    merged = settings_module.deep_merge(base, override)

    # Mutate the override after the fact
    override["b"]["nested"] = "mutated"

    assert merged["b"]["nested"] == "original", (
        "deep_merge aliased override nested dict into return value"
    )


def test_deep_merge_lists_replace_wholesale():
    """Lists (schedules in particular) must replace wholesale, not
    append. A user who sends a 2-item schedules list must end up
    with exactly 2 items, not 2 + whatever was there before."""
    base = {"schedules": [{"id": "old-1"}, {"id": "old-2"}]}
    override = {"schedules": [{"id": "new-1"}]}

    merged = settings_module.deep_merge(base, override)
    assert len(merged["schedules"]) == 1
    assert merged["schedules"][0]["id"] == "new-1"


def test_deep_merge_underscore_alias_still_works():
    """Back-compat: _deep_merge should still be importable for code
    that hasn't migrated to the public `deep_merge` name yet."""
    assert settings_module._deep_merge is settings_module.deep_merge