FROM python:3.11-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN uv sync --no-dev
ENV PROVGATE_DB_PATH=/data/provgate.db
VOLUME ["/data"]
ENTRYPOINT ["uv", "run", "provgate"]
CMD ["sync", "--all"]
