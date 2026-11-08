# AFClaude manager container: runs AFClaude's schedule (the resident runner, runner.py: sampler,
# window starts, keep-alive passes, usage review, quickview export, dispatcher, ...) and its MCP server.
# Everything that touches Claude sessions still runs ON THE HOST, through the SSH
# bridge (host.py -> docker/host_exec.py). See README.md, "Manager container".
# Build context: the repo root (docker/compose.yml sets it).
# The base image is pinned by digest (docs/phase5b_plan.md K1); to move it, pull the new tag, put the new
# digest here AND in the comment of docker/requirements.txt, rebuild, run the suites.
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

# The code lives at /app, whatever the host's checkout path is: no host path is baked into the image.
# What the host must understand (a project's directory, a launch cwd, the paths named in a message to a
# session) comes from the host repo's path the compose file hands in at run time (host.repo_dir()).
ARG APP=/app
ARG UID=1000
ARG GID=1000

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends openssh-client tini procps tzdata ca-certificates \
        git tmux; \
    rm -rf /var/lib/apt/lists/*; \
    groupadd -g "${GID}" afclaude; useradd -u "${UID}" -g "${GID}" -m -d /home/afclaude -s /bin/bash afclaude

# git + tmux are for the unit tests only (test_check_public, test_keepalive); AFClaude
# itself never runs tmux here: with AFCLAUDE_IN_CONTAINER set, host.py sends tmux calls to the host.
# The Python dependencies, pinned with hashes (docker/requirements.in -> requirements.txt): the MCP
# server's SDK and what it pulls in. --require-hashes refuses any package that is not pinned.
COPY docker/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt && rm /tmp/requirements.txt

# The code, owned by root: the manager cannot change what it runs.
COPY . ${APP}
# Local-only / live files are read from the host repo (mounted read-only at
# /host/repo, incl. the local backlog/ briefs); PROGRESS.md and ALERTS.md are appended to through the bridge.
# data/ is a read-write mount of the host repo's data/. (Until phase 5b K4 replaces these mounts.)
RUN set -eux; cd "${APP}"; \
    for f in PROGRESS.md ALERTS.md BACKLOG.md GOALS.md EXCEPTIONS.md README.md; do \
      rm -f "$f"; ln -s "/host/repo/$f" "$f"; \
    done; \
    rm -rf backlog; ln -s /host/repo/backlog backlog; \
    rm -rf data; mkdir -m 755 data; chown "${UID}:${GID}" data; \
    chmod 755 docker/at_shim.py; ln -s "${APP}/docker/at_shim.py" /usr/local/bin/at; \
    install -m 755 docker/entrypoint.sh /usr/local/bin/afclaude-entrypoint; \
    chmod 755 docker/entrypoint.sh afclaude.py; \
    ln -s "${APP}/afclaude.py" /usr/local/bin/afclaude; \
    python3 -m compileall -q . >/dev/null || true

# The commit the image is built from (the image has no .git): actions._code_version reads
# CODE_VERSION. compose.yml passes it from the shell, see its Start line; empty -> no file.
ARG AFCLAUDE_CODE_VERSION=
RUN set -eu; cd "${APP}"; rm -f CODE_VERSION; \
    if [ -n "${AFCLAUDE_CODE_VERSION}" ]; then printf '%s\n' "${AFCLAUDE_CODE_VERSION}" > CODE_VERSION; fi

# HOME is the container user's own; compose.yml sets it to the host user's home (a host path, from
# docker/.env) while the host's transcript dirs are mounted at the same path.
ENV AFCLAUDE_IN_CONTAINER=1 \
    KEEPALIVE_STATE_DIR=${APP}/data/keepalive \
    AFCLAUDE_BRIDGE_KEY=/run/secrets/host_bridge_ed25519 \
    AFCLAUDE_BRIDGE_KNOWN_HOSTS=/tmp/afclaude_known_hosts \
    HOME=/home/afclaude \
    LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1

USER ${UID}:${GID}
WORKDIR ${APP}
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/afclaude-entrypoint"]
