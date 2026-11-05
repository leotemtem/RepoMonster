FROM python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30 AS requirements

WORKDIR /build
RUN pip install --no-cache-dir uv==0.11.28
COPY pyproject.toml uv.lock README.md ./
RUN uv export --frozen --no-dev --no-emit-project --extra db --extra web --output-file requirements.txt

FROM python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    REPOMONSTER_ROOT=/app

WORKDIR /app

COPY --from=requirements /build/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps .

COPY db ./db
COPY profiles ./profiles
COPY standard-packs ./standard-packs

USER 65532:65532

EXPOSE 8000

CMD ["uvicorn", "repomonster.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
