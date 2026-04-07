FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    POETRY_NO_INTERACTION=1

WORKDIR /app

RUN pip install --no-cache-dir poetry==2.1.4

COPY pyproject.toml ./
RUN poetry install --only main

COPY app.py ./

ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
