FROM python:3.12-slim-bookworm

COPY requirements.txt /tmp/requirements.txt

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        fonts-liberation \
        fonts-noto-cjk \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --requirement /tmp/requirements.txt \
    && pip check

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /mnt/data /work \
    && chown -R app:app /mnt/data /work

USER app
ENV HOME=/home/app
ENV MPLCONFIGDIR=/tmp/matplotlib
WORKDIR /mnt/data

CMD ["sleep", "infinity"]
