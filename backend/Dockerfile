FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/srv

WORKDIR /srv

# Build deps for lxml and bcrypt, removed in the same layer.
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libxml2-dev libxslt1-dev \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && apt-get purge -y gcc && apt-get autoremove -y

RUN apt-get update && apt-get install -y --no-install-recommends git \
 && git clone https://github.com/hectorjacobo-dh/dh-sanctions-screening.git /tmp/repo \
 && mkdir -p /srv/app \
 && cp -r /tmp/repo/backend/app/* /srv/app/ \
 && ls -la /srv/app | head -5 \
 && rm -rf /tmp/repo /var/lib/apt/lists/* \
 && apt-get purge -y git && apt-get autoremove -y

# Run unprivileged.
RUN useradd --system --uid 10001 screening \
 && mkdir -p /srv/data \
 && chown -R screening:screening /srv
USER screening

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
