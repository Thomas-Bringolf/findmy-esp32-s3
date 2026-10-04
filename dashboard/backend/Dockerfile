FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Data (devices.json + account_state.json) is mounted read-only from the host.
ENV DATABASE_URL="" STATE_DIR=/data API_PORT=8090 FETCH_INTERVAL=900 VERBOSE=0

EXPOSE 8090
CMD ["python", "-u", "app.py"]
