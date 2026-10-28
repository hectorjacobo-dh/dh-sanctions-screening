FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PYTHONPATH=/srv

WORKDIR /srv

RUN apt-get update && apt-get install -y gcc libxml2-dev libxslt1-dev && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && apt-get purge -y gcc && apt-get autoremove -y

RUN apt-get update && apt-get install -y git && \
    git clone --depth 1 --single-branch --branch main https://github.com/hectorjacobo-dh/dh-sanctions-screening.git /tmp/repo && \
    git -C /tmp/repo log --oneline -1 && \
    cp -r /tmp/repo/backend/app /srv/app && \
    cp /tmp/repo/start.sh /srv/start.sh && \
    chmod +x /srv/start.sh && \
    rm -rf /tmp/repo && \
    apt-get purge -y git && \
    apt-get autoremove -y

RUN useradd --system --uid 10001 screening && mkdir -p /srv/data /data && chown -R screening:screening /srv /data
USER screening

EXPOSE 8000

ENTRYPOINT ["/srv/start.sh"]
