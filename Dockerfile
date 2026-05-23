FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml ./
COPY cli ./cli
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir -e .

COPY config.py config.yaml ./
COPY engine ./engine
COPY registry ./registry
COPY scripts ./scripts

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV FORGE_CONFIG=/app/config.yaml

EXPOSE 8000 8001
