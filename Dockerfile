# Sribees Price Intel API. One image runs both the API (uvicorn) and the daily
# scheduler (python -m app.cli scheduler); the compose file picks the command.
# bookworm: the Debian release Playwright supports (trixie lacks its font packages).
FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so a code-only change reuses this layer.
COPY pyproject.toml ./
RUN mkdir app && touch app/__init__.py \
    && pip install . \
    && pip uninstall -y sribees-price-intel \
    && rm -rf app

# Chromium for the shops whose prices only appear after their page's JavaScript runs.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN python -m playwright install --with-deps chromium && chmod -R a+rX /ms-playwright

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin intel
USER intel

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
