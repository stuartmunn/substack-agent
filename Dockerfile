FROM python:3.12.8-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

RUN useradd --system --uid 10001 --no-create-home humblebrag \
    && mkdir /data \
    && chown humblebrag:humblebrag /data
USER humblebrag

VOLUME /data
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=1 \
    CMD ["python", "-m", "humblebrag.heartbeat"]
CMD ["python", "-m", "humblebrag"]
