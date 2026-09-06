# RecallOps agent backend ("agent VM"). git is required by the clone-based fixer.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 recallops

WORKDIR /srv/app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

RUN mkdir -p /srv/app/storage/workspaces && chown -R recallops:recallops /srv/app
USER recallops

ENV WORKSPACES_DIR=/srv/app/storage/workspaces
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
