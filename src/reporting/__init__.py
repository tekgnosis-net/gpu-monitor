"""
GPU Monitor reporting subpackage.

Phase 6 of the v1.0.0 overhaul. Groups the modules responsible for
settings persistence, SMTP password encryption, email rendering,
mail delivery, and scheduled-report execution.

Module map:
    settings.py   — Pydantic models + atomic load/save for settings.json
    crypto.py     — Fernet wrapper for SMTP password at rest
    render.py     — Jinja2 + matplotlib PNG charts for HTML email bodies
    mailer.py     — aiosmtplib wrapper for STARTTLS / TLS / plain send
    scheduler.py  — standalone cron-driven report runner (supervised subprocess)

Kept as a subpackage rather than a flat src/reporting.py so individual
tests can import just the piece they need without dragging in the rest
(e.g. tests/test_crypto.py doesn't need to import matplotlib).
"""
