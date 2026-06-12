FROM python:3.11.15-slim-trixie@sha256:f9fa7f851e38bfb19c9de3afbc4b86ae7176ea7aaf94535c31df5458d5849457

WORKDIR /app

COPY pyproject.toml /app/pyproject.toml
COPY app /app/app

RUN pip install --no-cache-dir .

EXPOSE 8212

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8212"]
