# Omnissa Horizon MCP server — containerised.
# Runs as a non-root user. stdio (embedded MCP client) by default; override the
# CMD to run as a network daemon: --transport http|sse|streamable-http.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Create an unprivileged runtime user.
RUN groupadd -r horizon && useradd -r -g horizon -d /app horizon

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source.
COPY omnissa_horizon_mcp ./omnissa_horizon_mcp
RUN python -m compileall -q omnissa_horizon_mcp

USER horizon

# HTTP/SSE transports bind here.
EXPOSE 8000

# Default: stdio transport. Override CMD for a daemon:
#   docker run -p 8000:8000 omnissa-horizon-mcp --transport http --port 8000
ENTRYPOINT ["python", "-m", "omnissa_horizon_mcp"]
CMD []

# Healthcheck: a lightweight TCP probe of the entrypoint (best-effort; only
# meaningful when running with --transport http/sse). Located on the default port.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import socket,sys; s=socket.create_connection(('127.0.0.1', %d), 6); s.close()" % 8000 || exit 1