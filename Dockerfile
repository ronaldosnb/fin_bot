FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv/finbot
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY migrations migrations
COPY alembic.ini .
RUN useradd --system --uid 10001 --create-home finbot && chown -R finbot:finbot /srv/finbot
USER finbot
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
