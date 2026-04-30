"""
Pytest collection config for the gpu-monitor test tree.

Phase 3 introduces tests/test_api.py as the first proper pytest module,
but the tests/ directory already contains older standalone load-test
scripts (db_load_*.py, test_trimming.py) that pre-date the pytest
harness. Those scripts use psutil and expect a real running database
rather than a pytest fixture; they would fail at collection time here.

The `collect_ignore_glob` list below tells pytest to skip those legacy
files during collection. Future phases can port them into proper
pytest modules and remove their names from this list.
"""

import sys
from pathlib import Path

# Make `src/` importable for tests so `from gpu_monitor import db` works
# without requiring an editable install. server_module and reporting/
# tests already do this inline; doing it once here keeps the new
# gpu_monitor tests clean.
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

collect_ignore_glob = [
    "db_load_*.py",   # db_load_test.py, db_load_3d_test.py, db_load_7d_test.py
    "test_trimming.py",
]
