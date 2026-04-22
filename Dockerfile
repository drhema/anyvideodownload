# Microsoft's official Playwright image for Python: Ubuntu Noble + Chromium +
# all OS libs Chromium needs. Saves us from installing ~80MB of system packages.
FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

# ffmpeg (for mux) + Xvfb (virtual X display so Chromium can run "headed").
# Headed mode avoids the bot-detection many sites apply to headless Chrome.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg xvfb tini x11-utils curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN pip install --no-cache-dir -e . \
    && chmod +x /usr/local/bin/docker-entrypoint.sh

# The Playwright base image ships with a pwuser account already configured
# for browser permissions — reuse it. Just create /data and hand ownership over.
RUN mkdir -p /data \
    && chown -R pwuser:pwuser /data /app

USER pwuser
WORKDIR /home/pwuser

# Environment defaults
ENV PYVID_STORAGE=/data \
    PYVID_HOST=0.0.0.0 \
    PYVID_PORT=8000 \
    PYVID_CONCURRENCY=1 \
    PYVID_RATE_LIMIT=10 \
    PYVID_CHROMIUM_ARGS="--no-sandbox --disable-dev-shm-usage" \
    DISPLAY=:99 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

EXPOSE 8000

# tini is PID 1 so Ctrl-C and SIGTERM propagate cleanly.
ENTRYPOINT ["tini", "--", "docker-entrypoint.sh"]
CMD ["pyvid-api"]
