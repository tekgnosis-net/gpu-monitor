"""Async GPU metric collector loop.

Replaces the bash `update_stats()` tick + `process_buffer()` flush.
Direct per-tick INSERT under SQLite WAL — no buffer staging, no
retry ladder, no audit log (see plan rationale: WAL contention is
sub-millisecond and almost never fails; the batch-flush loss model
that motivated the bash machinery is gone).

Filled in during Step 4 of the refactor.
"""
