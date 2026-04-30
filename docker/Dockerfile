FROM python:3.13.3-slim-bookworm

# Install required packages.
#
# v2.0.0 dropped:
#   * jq        — settings/inventory JSON now built by Python's json module
#   * sqlite3   — schema migrations now run via Python's sqlite3 module
#   * curl      — was a Phase-1 remnant, no consumer in the codebase
#   * monitor_gpu.sh / bash collector — replaced by `python3 -m gpu_monitor`
#
# fonts-dejavu-core stays — matplotlib's email report charts need a
# predictable font stack to avoid bitmap-font fallback warnings.
RUN apt-get update && apt-get install -y \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies.
#
# Phase 3 baseline:  aiohttp (server)
# Phase 6a added:    pydantic (settings validation),
#                    cryptography (Fernet for SMTP password at rest)
# Phase 6b added:    aiosmtplib (async SMTP send),
#                    jinja2 + premailer (HTML email templating),
#                    matplotlib (PNG chart rendering for email),
#                    croniter (cron schedule evaluation)
# v2.0.0   added:    nvidia-ml-py (NVML bindings; replaces the
#                    `nvidia-smi` subprocess fork in the bash collector
#                    with direct calls into libnvidia-ml.so)
#
# --no-cache-dir keeps pip's download cache out of the final image
# layer.
RUN pip install --no-cache-dir \
    aiohttp \
    pydantic \
    cryptography \
    aiosmtplib \
    jinja2 \
    premailer \
    matplotlib \
    croniter \
    nvidia-ml-py

# Create app directory
WORKDIR /app

# Create runtime data directories
RUN mkdir -p /app/history /app/logs

# Copy application files
# Repo layout differs from container layout: src/web/ holds the frontend
# (HTML shell, ES modules, Lit components, design-token CSS, images,
# sounds) and src/ holds the bash collector and python server.
# Inside the container everything lives flat at /app/ so the static
# server in server.py resolves relative paths (images/, sounds/,
# styles/, components/, views/, app.js) the same way the browser does.
#
# Phase 4 restructures the frontend from a single 1200-line HTML file
# into a 50-line HTML shell plus ~15 ES modules and 3 CSS files. The
# COPY below preserves the directory hierarchy for app.js / styles/ /
# components/ / views/ so the browser's relative imports resolve.
COPY src/web/gpu-stats.html /app/
COPY src/web/app.js         /app/
COPY src/web/api.js         /app/
COPY src/web/theme.js       /app/
COPY src/web/router.js      /app/
COPY src/web/sidebar.js     /app/
COPY src/web/alerts.js      /app/
COPY src/web/keybindings.js /app/
COPY src/web/styles/       /app/styles/
COPY src/web/components/   /app/components/
COPY src/web/views/        /app/views/
COPY src/web/widgets/      /app/widgets/
COPY src/web/images/       /app/images/
COPY src/web/sounds/       /app/sounds/
COPY src/server.py         /app/
# Phase 6 — reporting subpackage (settings, crypto, mailer, render,
# scheduler + Jinja template). Copied as a directory tree so
# `from reporting import ...` resolves correctly when imported by the
# unified entrypoint at /app/.
COPY src/reporting/        /app/reporting/
# v2.0.0 — gpu_monitor package (collector, NVML source, db, inventory,
# housekeeping, lifecycle, __main__). Replaces the bash collector.
COPY src/gpu_monitor/      /app/gpu_monitor/

# PYTHONPATH=/app makes `import server`, `from reporting import …`, and
# `from gpu_monitor import …` all resolve from the unified entrypoint.
ENV PYTHONPATH=/app

# Application version: APP_VERSION is the SINGLE source of truth at build
# time. CI (see .github/workflows/docker-publish.yml) reads the tracked
# VERSION file at workflow time and plumbs it here; local builds can run
# `docker build --build-arg APP_VERSION=$(cat VERSION) ...` to match.
# Local builds that forget the flag get "dev" in both places — never a
# divergence between the OCI label and runtime /app/VERSION.
#
# Placed late in the Dockerfile so that layer caching for the COPYs above
# is not invalidated every time APP_VERSION changes.
ARG APP_VERSION=dev
LABEL org.opencontainers.image.version="${APP_VERSION}"
RUN printf '%s\n' "${APP_VERSION}" > /app/VERSION

# Expose port for web server
EXPOSE 8081

# Start the application
CMD ["python3", "-m", "gpu_monitor"]
