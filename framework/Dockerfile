FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
 && rm -rf /var/lib/apt/lists/*

COPY framework/requirements.txt /app/framework/requirements.txt
RUN pip install --no-cache-dir -r /app/framework/requirements.txt

COPY framework /app/framework
COPY monitoring /app/monitoring
COPY bots /app/bots
COPY scripts /app/scripts
COPY tests /app/tests
COPY pytest.ini /app/pytest.ini

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

CMD ["python", "-m", "framework.main"]
