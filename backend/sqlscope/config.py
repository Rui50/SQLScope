"""Settings, read from environment variables with local-dev defaults."""

import os

# admin connection to the sandbox Postgres from docker-compose.yml.
DATABASE_URL = os.environ.get(
    "SQLSCOPE_DATABASE_URL",
    "postgresql://postgres:sqlscope@localhost:5433/postgres",
)