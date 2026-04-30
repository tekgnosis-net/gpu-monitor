"""Periodic maintenance tasks: log rotation + DB purge.

Replaces the bash `rotate_logs()` (hourly, size+age based) and
`clean_old_data()` (daily, retention_days from settings).

Filled in during Step 5 of the refactor.
"""
