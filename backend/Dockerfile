# Elite Test Drive Booking — backend image (AD-G)
# python:3.11-slim base, non-root user, requirements installed before source so
# the dependency layer is cached across code changes.
FROM python:3.11-slim

# No .pyc files; unbuffered stdout so logs stream to `docker compose logs`.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 1) Dependencies first — this layer is reused until requirements.txt changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 2) Application source.
COPY app ./app

# 3) Seed package + media. NOT optional, despite being "fixtures":
#    - app/cli.py does `from seed import ...`, so `python -m app.cli reset-demo`
#      dies with ModuleNotFoundError without it. Seeding the demo database
#      inside the container is impossible.
#    - main.py serves /media from seed/media (rule 18, vehicle cards) and mounts
#      it ONLY IF PRESENT -- so a missing directory is not an error, it is nine
#      silently broken vehicle images.
#    Both failures are Docker-only: local dev runs from the repo where seed/ is
#    simply there, which is why neither showed up until first deploy.
COPY seed ./seed

# 4) Migrations. NOT optional, for exactly the reason seed/ above is not: main.py
#    runs `migrate.upgrade_to_head()` at startup, and alembic resolves its
#    script_location on disk. Without these the container imports fine, boots,
#    and dies in lifespan on a missing migration directory — a failure that
#    cannot happen in local dev, where the repo is simply there. Same class of
#    bug as the two documented above, caught before first deploy this time.
# The brochure PDFs, the brochure MD and the knowledge files. WITHOUT THIS the
# container boots fine and then 404s every brochure request while local dev
# works perfectly — the same shape as the missing `alembic` directory below,
# which is why both are pinned by tests in tests/test_migrations.py and
# tests/test_brochure_files.py.
COPY content ./content
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini

# Run as a non-root user (create after install so pip runs as root).
RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# --timeout-keep-alive 75 is required for SSE chat streaming (AD-D).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "75"]
