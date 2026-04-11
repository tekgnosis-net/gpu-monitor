FROM python:3.13.3-slim-bookworm

# Install required packages.
#
# fonts-dejavu-core gives matplotlib a predictable font stack for the
# email report charts (Phase 6). Without it matplotlib falls back to a
# bitmap font and logs warnings on every chart render, which both
# looks cheap in the email and clutters the collector log.
RUN apt-get update && apt-get install -y \
    curl \
    jq \
    sqlite3 \
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
#
# Image-size cost: ~80 MB on top of the Phase 3 baseline, dominated by
# matplotlib's numpy runtime. The alternative (SVG inline or external
# chart rendering) was considered and rejected — matplotlib's PNG +
# MIME multipart/related gives the best compatibility across Gmail,
# Outlook, and Apple Mail without per-client tuning.
RUN pip install \
    aiohttp \
    pydantic \
    cryptography \
    aiosmtplib \
    jinja2 \
    premailer \
    matplotlib \
    croniter

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
COPY src/web/styles/       /app/styles/
COPY src/web/components/   /app/components/
COPY src/web/views/        /app/views/
COPY src/web/images/       /app/images/
COPY src/web/sounds/       /app/sounds/
COPY src/monitor_gpu.sh    /app/
COPY src/server.py         /app/
# Phase 6 — reporting subpackage (settings, crypto, mailer, render,
# scheduler + Jinja template). Copied as a directory tree so
# `from reporting import ...` resolves correctly when server.py and
# the scheduler subprocess run at /app/ root.
COPY src/reporting/        /app/reporting/

# Make scripts executable
RUN chmod +x /app/monitor_gpu.sh

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
CMD ["./monitor_gpu.sh"]
