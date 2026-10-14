FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini start.sh ./
COPY migrations ./migrations
COPY app ./app

RUN useradd --create-home --uid 1000 app
USER app

EXPOSE 8000

# A variável ROLE escolhe o papel: api (padrão), worker ou beat.
CMD ["sh", "start.sh"]
