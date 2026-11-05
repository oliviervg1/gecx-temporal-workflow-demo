FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.6.3 /uv /usr/local/bin/uv
RUN uv venv /app/.venv && \
    uv pip install --python /app/.venv/bin/python \
      "fastapi>=0.115.0" \
      "uvicorn[standard]>=0.32.0" \
      "pydantic>=2.9.0" \
      "temporalio>=1.9.0" \
      "httpx>=0.27.0"

COPY temporal_service/ /app/temporal_service/
RUN /app/.venv/bin/python /app/temporal_service/warm_temporal.py

ENV PATH="/app/.venv/bin:${PATH}"
ENV PYTHONPATH="/app"
ENV PORT=8080
ENV TEMPORAL_DB_PATH=/tmp/albion_temporal.db
EXPOSE 8080

CMD ["/app/.venv/bin/uvicorn", "temporal_service.app:app", "--host", "0.0.0.0", "--port", "8080"]
