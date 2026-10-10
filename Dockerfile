FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd --create-home --uid 1000 app
COPY --chown=app:app . .
USER app

HEALTHCHECK --interval=60s --timeout=5s --start-period=60s \
  CMD python -c "import os,sys,time; sys.exit(time.time()-os.path.getmtime('/tmp/heartbeat')>180)"

CMD ["python", "-m", "app.main"]
