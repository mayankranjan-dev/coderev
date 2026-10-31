# Stage 1: build the frontend. Nothing from this stage except dist/ is shipped.
FROM node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Stage 2: the API, which also serves the built frontend (frontend_static.py).
FROM python:3.12-slim AS app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

# Runtime code only: no tests, eval harness, docs or .env (see .dockerignore).
COPY main.py models.py database.py security.py retry_queue.py frontend_static.py ./
COPY reviewer/ ./reviewer/
COPY prompts/ ./prompts/
COPY --from=frontend /build/dist ./frontend/dist

# Non-root user. /data holds the SQLite file and is meant to be a volume.
RUN useradd --system --uid 10001 --no-create-home coderev \
    && mkdir /data \
    && chown coderev:coderev /data
ENV DATABASE_URL=sqlite:////data/audit.db
VOLUME ["/data"]
USER coderev

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
