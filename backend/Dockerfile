FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 APP_MODE=web
WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 hong
COPY app/ ./
COPY entrypoint.py ./vps_main.py
USER hong
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
  CMD python -c "import json,urllib.request; r=json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=4)); assert r['ok'] and r['mode']=='web'"
CMD ["python", "-m", "uvicorn", "vps_main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers"]
