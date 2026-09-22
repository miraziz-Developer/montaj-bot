FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 app

WORKDIR /app

COPY pyproject.toml ./
# Install dependencies first (cached layer); the package itself is editable and reads /app at runtime.
RUN mkdir -p app && touch app/__init__.py \
    && pip install --no-cache-dir -e ".[dev]"

COPY . .
RUN mkdir -p /tmp/montaj && chown -R app:app /app /tmp/montaj

USER app
