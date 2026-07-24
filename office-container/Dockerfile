FROM python:3.12-slim-bookworm

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        fonts-liberation \
        fonts-noto-cjk \
        libreoffice-calc \
        libreoffice-impress \
        libreoffice-writer \
        pandoc \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir \
        openpyxl==3.1.5 \
        python-pptx==1.0.2

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
