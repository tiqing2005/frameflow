FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FRAMEFLOW_DATA_DIR=/data \
    FRAMEFLOW_DATABASE_URL=sqlite:////data/frameflow.db \
    PORT=8000

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

RUN mkdir -p /data && useradd --create-home --uid 10001 frameflow && chown -R frameflow:frameflow /app /data
USER frameflow

EXPOSE 8000
VOLUME ["/data"]
HEALTHCHECK --interval=20s --timeout=3s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/ready', timeout=2)" || exit 1

CMD ["python", "-m", "app.serve"]

