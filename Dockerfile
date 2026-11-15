FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend/ backend/
COPY frontend/feed frontend/feed
ENV PYTHONUNBUFFERED=1
EXPOSE 10000
CMD ["sh", "-c", "cd backend && uvicorn app.digest_main:app --host 0.0.0.0 --port ${PORT:-10000}"]
