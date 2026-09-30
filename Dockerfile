FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY live/ ./live/

RUN useradd -m -u 1000 trader \
    && mkdir -p /app/out \
    && chown -R trader:trader /app
USER trader

VOLUME ["/app/out"]

CMD ["python", "live/trader.py"]
