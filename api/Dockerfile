FROM python:3.10-slim

WORKDIR /app

COPY api/requirements.txt ./api/requirements.txt
RUN pip install --no-cache-dir -r api/requirements.txt

COPY shared/ ./shared/
COPY api/ ./api/
COPY alembic.ini ./alembic.ini
COPY migrations/ ./migrations/

ENV PYTHONUNBUFFERED=1

EXPOSE 5000

COPY api/entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

CMD ["./entrypoint.sh"]
