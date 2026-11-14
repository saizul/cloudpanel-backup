FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
# Programs the backup runs: mariadb-dump/mariadb, age, gpg, rsync and ssh.
RUN apt-get update \
 && apt-get install -y --no-install-recommends mariadb-client age gnupg rsync openssh-client ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install . && useradd --system --uid 10001 app
USER app
WORKDIR /work
ENTRYPOINT ["cloudpanel-backup"]
CMD ["--help"]
