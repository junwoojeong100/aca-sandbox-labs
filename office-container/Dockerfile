FROM python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

ARG DEBIAN_SNAPSHOT=20260720T000000Z

COPY requirements.txt /tmp/requirements.txt

RUN sed -i \
        -e "s|http://deb.debian.org/debian-security|https://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}|g" \
        -e "s|http://deb.debian.org/debian|https://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}|g" \
        /etc/apt/sources.list.d/debian.sources \
    && printf 'Acquire::Check-Valid-Until \"false\";\n' > /etc/apt/apt.conf.d/99snapshot \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        fonts-liberation \
        fonts-noto-cjk \
        libreoffice-calc \
        libreoffice-impress \
        libreoffice-writer \
        pandoc \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --requirement /tmp/requirements.txt \
    && pip check

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app /work \
    && chown -R app:app /app /work

COPY server.py /app/server.py

USER app
ENV HOME=/home/app
ENV PYTHONUNBUFFERED=1
WORKDIR /app
EXPOSE 8080

CMD ["python", "/app/server.py"]
