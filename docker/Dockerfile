# AFClaude manager container: runs AFClaude's schedule (the resident runner, runner.py: sampler,
# window starts, keep-alive passes, usage review, quickview export, dispatcher, ...) and its MCP server.
# Everything that touches Claude sessions still runs ON THE HOST, through the SSH
# bridge (host.py -> docker/host_exec.py). See README.md, "Manager container".
# Build context: the repo root (docker/compose.yml sets it).
FROM python:3.12-slim-bookworm

# Same path as the host repo: HERE-relative paths (cwd for ka_resume.sh, the
# PROGRESS.md path in continue messages, own-session markers) must be host paths.
ARG AFCLAUDE_DIR=/mnt/BlockVolume/Claude/work/AFClaude
ARG UID=1000
ARG GID=1000

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends openssh-client tini procps tzdata ca-certificates \
        git tmux; \
    rm -rf /var/lib/apt/lists/*; \
    groupadd -g "${GID}" opc; useradd -u "${UID}" -g "${GID}" -m -d /home/opc -s /bin/bash opc

# git + tmux are for the unit tests only (test_check_public, test_keepalive); AFClaude
# itself never runs tmux here: with AFCLAUDE_IN_CONTAINER set, host.py sends tmux calls to the host.
# The MCP server's SDK (same version as the host's .venv).
RUN pip install --no-cache-dir mcp==2.2.0

# The code, owned by root: the manager cannot change what it runs.
COPY . ${AFCLAUDE_DIR}
# Local-only / live files are read from the host repo (mounted read-only at
# /host/repo, incl. the local backlog/ briefs); PROGRESS.md and ALERTS.md are appended to through the bridge.
# data/ is a read-write mount of the host repo's data/.
RUN set -eux; cd "${AFCLAUDE_DIR}"; \
    for f in PROGRESS.md ALERTS.md OPEN_QUESTIONS.md BACKLOG.md GOALS.md EXCEPTIONS.md README.md DECISIONS.md; do \
      rm -f "$f"; ln -s "/host/repo/$f" "$f"; \
    done; \
    rm -rf backlog; ln -s /host/repo/backlog backlog; \
    rm -rf data; mkdir -m 755 data; chown "${UID}:${GID}" data; \
    install -m 755 docker/at_shim.py /usr/local/bin/at; \
    install -m 755 docker/entrypoint.sh /usr/local/bin/afclaude-entrypoint; \
    chmod 755 docker/entrypoint.sh; \
    python3 -m compileall -q . >/dev/null || true

# The commit the image is built from (the image has no .git): actions._code_version reads
# CODE_VERSION. compose.yml passes it from the shell, see its Start line; empty -> no file.
ARG AFCLAUDE_CODE_VERSION=
RUN set -eu; cd "${AFCLAUDE_DIR}"; rm -f CODE_VERSION; \
    if [ -n "${AFCLAUDE_CODE_VERSION}" ]; then printf '%s\n' "${AFCLAUDE_CODE_VERSION}" > CODE_VERSION; fi

ENV AFCLAUDE_IN_CONTAINER=1 \
    AFCLAUDE_DIR=${AFCLAUDE_DIR} \
    KEEPALIVE_STATE_DIR=${AFCLAUDE_DIR}/data/keepalive \
    AFCLAUDE_BRIDGE_KEY=/run/secrets/host_bridge_ed25519 \
    AFCLAUDE_BRIDGE_KNOWN_HOSTS=/tmp/afclaude_known_hosts \
    HOME=/home/opc \
    LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1

USER ${UID}:${GID}
WORKDIR ${AFCLAUDE_DIR}
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/afclaude-entrypoint"]
