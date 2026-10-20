# Ephemeral GitHub Actions runner image (linux/arm64 and linux/amd64).
# Built natively on the worker VM by deploy/install.sh - BuildKit's predefined
# TARGETARCH build arg selects the matching agent and docker apt repo.
#
# Contents:
#   * official actions/runner agent (pinned + checksum-verified per arch)
#   * docker CLI + buildx + compose plugins -> jobs can build images through
#     the mounted host docker.sock (sibling containers)
#   * small set of universally useful build tools; workflows install the rest
FROM ubuntu:24.04

# TARGETARCH is auto-populated by BuildKit (arm64 on A1, amd64 on x86_64 hosts);
# deploy/install.sh also passes it explicitly as a safety net.
ARG TARGETARCH
ARG RUNNER_VERSION=2.338.0
# ^^ per-arch sha256 checksums of actions-runner-linux-<arch>-${RUNNER_VERSION}.tar.gz
#    (release v2.338.0, 2026-10-06) are pinned inside the fetch RUN step.

LABEL oci-runner.keep=1 \
      org.opencontainers.image.title="oci-github-runner runner" \
      org.opencontainers.image.description="Ephemeral GitHub Actions runner (arm64/amd64) with docker CLI"

ENV DEBIAN_FRONTEND=noninteractive \
    RUNNER_ALLOW_RUNASROOT=0

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git jq libicu74 tar gzip unzip zip gnupg \
      openssh-client lsb-release sudo python3-minimal \
      xz-utils bzip2 file \
      build-essential pkg-config \
    && rm -rf /var/lib/apt/lists/*
# ^^ CI baseline matching what ubuntu-latest jobs assume:
#    xz/bzip2 (release tarballs), build-essential + pkg-config (Rust and
#    other builds need a default linker `cc`; ubuntu-latest ships one).

# Docker CLI + buildx + compose plugins from Docker's official apt repo (native arch)
RUN install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
       | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=${TARGETARCH} signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
       > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli docker-buildx-plugin docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

# Non-root runner user with passwordless sudo (jobs often need apt installs)
RUN useradd -m -u 1001 -s /bin/bash runner \
    && echo "runner ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/runner \
    && chmod 0440 /etc/sudoers.d/runner \
    && mkdir -p /home/runner/actions-runner /home/runner/_work \
    && chown -R runner:runner /home/runner

WORKDIR /home/runner/actions-runner

# Official runner agent, pinned + verified per arch.
# set -eu keeps sha256sum -c fatal: a bad download MUST fail the build
# (the old `... || true` chaining silently masked checksum failures).
# NOTE: GitHub release assets use "x64" where BuildKit/debian use "amd64".
RUN set -eu; \
    case "${TARGETARCH}" in \
      arm64) AGENT_ARCH=arm64; RUNNER_CHECKSUM=628b4a7258487b80c1d3c221095a7ded349f5ae0175fd3dfab708d07428f041b ;; \
      amd64) AGENT_ARCH=x64;   RUNNER_CHECKSUM=af4b794c1bc41d73d40535e3fe092a39f9679cd8d965954c2aca25a05ca41d32 ;; \
      *) echo "ERROR: unsupported TARGETARCH='${TARGETARCH}' (build with BuildKit or pass --build-arg TARGETARCH)" >&2; exit 1 ;; \
    esac; \
    curl -fsSLo runner.tar.gz \
      "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-${AGENT_ARCH}-${RUNNER_VERSION}.tar.gz"; \
    echo "${RUNNER_CHECKSUM}  runner.tar.gz" | sha256sum -c -; \
    tar xzf runner.tar.gz; \
    rm runner.tar.gz; \
    ./bin/installdependencies.sh || true; \
    chown -R runner:runner /home/runner/actions-runner

COPY --chown=runner:runner entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

USER runner
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
