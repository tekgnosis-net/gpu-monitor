FROM python:3.13.3-slim-bookworm

# Install required packages
RUN apt-get update && apt-get install -y \
    curl \
    jq \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Install aiohttp
RUN pip install aiohttp

# Create app directory
WORKDIR /app

# Create runtime data directories
RUN mkdir -p /app/history /app/logs

# Copy application files
# Repo layout differs from container layout: src/web/ holds the frontend
# (HTML, images, sounds) and src/ holds the bash collector and python server.
# Inside the container everything lives flat at /app/ so that the static
# server resolves relative paths (images/, sounds/) the same way the browser does.
COPY src/web/gpu-stats.html /app/
COPY src/web/images/       /app/images/
COPY src/web/sounds/       /app/sounds/
COPY src/monitor_gpu.sh    /app/
COPY src/server.py         /app/

# Make scripts executable
RUN chmod +x /app/monitor_gpu.sh

# Expose port for web server
EXPOSE 8081

# Start the application
CMD ["./monitor_gpu.sh"]
