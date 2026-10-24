FROM python:3.10-slim

WORKDIR /app

COPY worker/requirements.txt ./worker/requirements.txt
RUN pip install --no-cache-dir -r worker/requirements.txt

COPY shared/ ./shared/
COPY worker/ ./worker/
COPY alembic.ini ./alembic.ini
COPY migrations/ ./migrations/

ENV PYTHONUNBUFFERED=1

RUN chmod +x ./worker/entrypoint.sh

CMD ["./worker/entrypoint.sh"]
