FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
COPY alembic.service.ini ./
COPY migrations/service ./migrations/service

RUN pip install --no-cache-dir .

EXPOSE 8000

CMD ["uvicorn", "profitsamurai.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
