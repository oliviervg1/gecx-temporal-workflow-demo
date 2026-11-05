FROM python:3.12-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.6.3 /uv /usr/local/bin/uv
RUN uv venv /app/.venv && \
    uv pip install --python /app/.venv/bin/python \
      "cxas-scrapi==1.10.0" \
      "google-auth>=2.35.0" \
      "requests>=2.32.0" \
      "fastapi>=0.115.0" \
      "uvicorn[standard]>=0.32.0" \
      "pydantic>=2.9.0" \
      "temporalio>=1.9.0" \
      "httpx>=0.27.0"

COPY cxas_app /app/cxas_app
COPY temporal_service /app/temporal_service
COPY demo_ui /app/demo_ui

ENV PATH="/app/.venv/bin:${PATH}"
ENV PYTHONPATH="/app"
ENV PORT=8080
EXPOSE 8080

CMD ["/app/.venv/bin/python", "-m", "demo_ui.server", "--host", "0.0.0.0"]
