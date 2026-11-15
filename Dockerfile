# One container: builds the PWA, then FastAPI serves API + static files on one port.
# Port 7860 = Hugging Face Spaces (Docker) default; other hosts: set PORT.
FROM node:20-slim AS web
WORKDIR /web
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend/ backend/
COPY --from=web /web/dist frontend/dist
ENV PYTHONUNBUFFERED=1
EXPOSE 7860
CMD ["sh", "-c", "cd backend && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860}"]
