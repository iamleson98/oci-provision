FROM python:3.12-alpine

# docker-cli lets the controller spawn sibling runner containers through the
# mounted /var/run/docker.sock (no docker daemon inside this container).
RUN apk add --no-cache docker-cli

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py github_api.py docker_manager.py autoscaler.py status.py main.py waker.py ./

LABEL oci-runner.keep=1 \
      org.opencontainers.image.title="oci-github-runner controller" \
      org.opencontainers.image.description="Queue-aware autoscaler for ephemeral GitHub Actions runners"

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1

CMD ["python", "-u", "main.py"]
