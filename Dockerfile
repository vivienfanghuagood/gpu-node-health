# The agent has zero third-party dependencies on purpose: it runs on nodes
# that are already sick, and it must not become another thing to debug during
# an incident. A plain slim base is the whole image - notably NOT a ROCm image,
# which would be ~10GB and slow to pull onto a degraded node.
FROM python:3.12-slim

# Do not write .pyc into the read-only-ish container fs; flush logs immediately
# so kubectl logs shows the last line before a crash.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY agent/gpu_health_agent /app/gpu_health_agent

# Runs unprivileged. Host access comes from the pod spec, not from root here:
# /proc/<pid>/stat is world-readable, and /var/log/kern.log is root:adm 0640,
# so membership in gid 4 (adm) via supplementalGroups is enough. An agent that
# needed root on every GPU node would be a worse security trade than the
# incident it prevents.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin agent
USER 10001

EXPOSE 9101
ENTRYPOINT ["python", "-m", "gpu_health_agent.main"]
