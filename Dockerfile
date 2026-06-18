# ---------------------------------------------------------------
# Swiss Ephemeris microservice (FastAPI / pyswisseph)
# Build context = repo root:  docker build -t swisseph-service .
# ---------------------------------------------------------------
FROM python:3.12-slim

WORKDIR /app

# Build toolchain: pyswisseph and cffi (timezonefinder) compile C extensions,
# which the slim image lacks a compiler for. Install, build, then remove to
# keep the final image small.
COPY requirements.txt ./
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential python3-dev libffi-dev \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove build-essential python3-dev libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Service code + ephemeris data files (main.py resolves ./ephe via __file__)
COPY . .

ENV SWISSEPH_EPHE_PATH=/app/ephe

EXPOSE 9047

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9047"]
