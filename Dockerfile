# adbc-replicate-hardcharger container image: the CLI plus every source/target driver.
# Debian bookworm (glibc 2.36); the Hardcharger driver's library needs glibc >= 2.17.
FROM python:3.13-slim-bookworm

LABEL org.opencontainers.image.title="adbc-replicate-hardcharger" \
      org.opencontainers.image.description="Config-driven ADBC-to-ADBC replication into Hardcharger" \
      org.opencontainers.image.source="https://github.com/gizmodata/adbc-replicate-hardcharger" \
      org.opencontainers.image.vendor="GizmoData LLC" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /opt/adbc-replicate-hardcharger
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# Wheels of dependencies not on PyPI yet (see wheels/README.md), found alongside PyPI.
COPY wheels ./wheels
RUN pip install --find-links ./wheels ".[all]" && adbc-replicate-hardcharger --version

# Run as an unprivileged user; configs are mounted read-only (e.g. /config/replicate.yaml).
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin replicate
USER 10001
WORKDIR /home/replicate

ENTRYPOINT ["adbc-replicate-hardcharger"]
CMD ["--help"]
