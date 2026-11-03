FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY configs ./configs
COPY replicagent ./replicagent
COPY scripts ./scripts
COPY tests ./tests
RUN pip install --no-cache-dir '.[test,cloud]' && rm -rf build *.egg-info && python -m pytest -q

CMD ["python", "-m", "replicagent.cloud"]
