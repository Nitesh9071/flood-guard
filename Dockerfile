FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FLOODGUARD_DB_PATH=/data/floodguard.db \
    FLOODGUARD_MODEL_PATH=/data/models/bengaluru_flood_model.joblib \
    FLOODGUARD_MODEL_META_PATH=/data/models/bengaluru_flood_model_meta.json
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py train_model.py frontend ./
RUN mkdir -p /data/models
EXPOSE 8000
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
