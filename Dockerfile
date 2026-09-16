# Mawaid (DentalDesk) — production container image.
#
# Builds a self-contained image that runs the FastAPI webhook server (which
# also starts the LangGraph agent's background message consumer). Works on
# Railway, Render, Fly.io, or any host that can run a Docker container.
#
# The SQLite database lives at /app/data/dentaldesk_app.db inside the
# container — mount a persistent volume at /app/data on your host so it
# survives restarts and redeploys, otherwise patient/appointment data will
# be lost every time the container restarts.

FROM python:3.13-slim

# uv is the package manager this project uses (see pyproject.toml / uv.lock)
RUN pip install --no-cache-dir uv

WORKDIR /app

# Copy dependency files first so Docker can cache this layer and skip
# re-installing dependencies when only application code changes. README.md
# is required here too — pyproject.toml declares it as the package readme,
# and the build step validates it exists.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev

# Now copy the rest of the application code.
COPY . .

# Ensure the data directory exists even before a volume is mounted there
# (first run without a volume, or local `docker run` testing).
RUN mkdir -p /app/data /app/logs

# Cloud hosts typically inject the real port via the PORT env var at
# runtime (see src/app/main.py) — this EXPOSE is documentation/for local
# `docker run -p`, not a hard requirement.
EXPOSE 8000

CMD ["uv", "run", "python", "-m", "app.main"]
