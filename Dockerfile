FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt pyproject.toml ./
COPY cli ./cli
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir -e .

COPY config.py config.yaml ./
COPY engine ./engine
COPY registry ./registry

ENV PYTHONUNBUFFERED=1

CMD ["uvicorn", "engine.main:app", "--host", "0.0.0.0", "--port", "8000"]
