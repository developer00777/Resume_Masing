FROM python:3.12-slim

# PyMuPDF ships manylinux wheels, so no system build deps are required for the slim base.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Railway injects $PORT; default to 8000 for local `docker run`.
ENV PORT=8000
EXPOSE 8000

# --proxy-headers + --forwarded-allow-ips='*': Railway terminates TLS at its
# edge and forwards to this container over plain HTTP, so without this the
# app sees every request as http:// even though the real page loaded over
# https://. That made Jinja2's url_for('static', ...) emit http:// URLs for
# CSS/JS on an https:// page -- browsers correctly block that as mixed
# content, so /candidate/MaskProfileIndex loaded with zero styling and zero
# JavaScript (the "button isn't working" report). '*' is safe here: Railway's
# proxy is the only thing that can reach this container's port at all.
# Shell form so $PORT expands at runtime.
CMD uvicorn app.server:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'
