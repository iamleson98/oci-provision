# Ephemeral GitHub Actions runner image (linux/arm64).
# Built natively on the Ampere A1 worker VM by deploy/install.sh.
#
# Contents:
#   * official actions/runner agent (pinned + checksum-verified), arm64
#   * docker CLI + buildx + compose plugins -> jobs can build images through
#     the mounted host docker.sock (sibling containers)
#   * small set of universally useful build tools; workflows install the rest
FROM ubuntu:24.04

ARG RUNNER_VERSION=2.338.0
ARG RUNNER_CHECKSUM=628b4a7258487b80c1d3c221095a7ded349f5ae0175fd3dfab708d07428f041b
# ^^ sha256 of actions-runner-linux-arm64-${RUNNER_VERSION}.tar.gz (release v2.338.0, 2026-10-06)

LABEL oci-runner.keep=1 \
      org.opencontainers.image.title="oci-github-runner runner" \
      org.opencontainers.image.description="Ephemeral GitHub Actions runner (arm64) with docker CLI"

ENV DEBIAN_FRONTEND=noninteractive \
    RUNNER_ALLOW_RUNASROOT=0

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git jq libicu74 tar gzip unzip zip gnupg \
      openssh-client lsb-release sudo python3-minimal \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI + buildx + compose plugins from Docker's official arm64 apt repo
RUN install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
       | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=arm64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
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

# Official runner agent, pinned + verified
RUN curl -fsSLo runner.tar.gz \
      "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-arm64-${RUNNER_VERSION}.tar.gz" \
    && echo "${RUNNER_CHECKSUM}  runner.tar.gz" | sha256sum -c - \
    && tar xzf runner.tar.gz \
    && rm runner.tar.gz \
    && ./bin/installdependencies.sh || true \
    && chown -R runner:runner /home/runner/actions-runner

COPY --chown=runner:runner entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

USER runner
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
