# Transition Agent — container image
# Note: the Windows WAM broker is unavailable in Linux containers, so in a
# container the app authenticates via the device-code flow (or a supplied
# WORKIQ_TOKEN). Mount a volume for /app/data to persist handover briefs.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=3000

WORKDIR /app

# Install runtime deps first for better layer caching. Strip the msal[broker]
# extra: it pulls pymsalruntime (Windows/macOS only, no Linux wheel). The web
# app signs in via the confidential-client auth-code flow, not the OS broker,
# so plain msal is sufficient — and this keeps requirements.txt the single
# source of truth, including the Azure SDKs (Cosmos, Blob, Identity) the app
# needs at runtime.
COPY requirements.txt .
RUN sed 's/msal\[broker\]/msal/' requirements.txt > requirements.linux.txt \
    && pip install -r requirements.linux.txt

COPY . .

# Drop privileges
RUN useradd --create-home appuser && chown -R appuser /app
USER appuser

EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3000/healthz').status==200 else 1)"

CMD ["python", "serve.py"]
