# "Project VM" container: Flask app supervised by the RecallOps sidecar watcher.
# Build context is the ima-agent-backend-python repo root.
FROM python:3.12-slim

WORKDIR /app
COPY demo-apps/flaky-notes/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt httpx
COPY demo-apps/flaky-notes/ ./
COPY sidecar/watcher.py /sidecar/watcher.py

ENV APP_CMD="python app.py" \
    HEALTH_URL="http://localhost:5000/health" \
    PORT=5000

EXPOSE 5000
ENTRYPOINT ["python", "/sidecar/watcher.py"]
