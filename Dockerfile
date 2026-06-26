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

# Shell form so $PORT expands at runtime.
CMD uvicorn app.server:app --host 0.0.0.0 --port ${PORT}
