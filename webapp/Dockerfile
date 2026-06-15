# Build context is the project root (dm/), not webapp/.
# docker-compose.yml sets: build.context: . and build.dockerfile: webapp/Dockerfile

FROM python:3.13-slim

WORKDIR /app

# libpq-dev + gcc for psycopg2; no other system deps needed
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (layer cache friendly)
COPY webapp/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Data generation script (used by the seeder service)
COPY generate_data.py ./generate_data.py

# Seeder (used by the seeder service only)
COPY docker/seed.py ./docker/seed.py

# Web application
COPY webapp/main.py ./main.py
COPY webapp/templates/ ./templates/

EXPOSE 8765

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8765"]
