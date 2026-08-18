FROM python:3.13-slim

WORKDIR /app

# git is needed to install the Aegis client from its GitHub tag. Both it and
# byteforge-aegis-models are PUBLIC repos, so no CR_PAT and no BuildKit secret
# mount are required here — if either ever goes private, switch to the
# --mount=type=secret pattern rather than ARG/ENV, which would bake the live
# token into docker history where anyone who can pull the image can read it.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY aegis_mcp_server.py .

RUN useradd --create-home appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

ENV MCP_TRANSPORT=streamable-http
ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8000
# FastMCP reads these two specifically. The server also passes host/port to
# the constructor, which is what actually makes the bind take effect —
# constructor args win over these env vars, so setting only the env would
# leave the container listening on 127.0.0.1 and unreachable.
ENV FASTMCP_HOST=0.0.0.0
ENV FASTMCP_PORT=8000

# Connect via the container's own hostname, NOT 127.0.0.1. A loopback probe
# succeeds even when FastMCP has bound only to 127.0.0.1 — the exact failure
# this image is most likely to hit — so it would report healthy while nginx
# gets connection-refused. gethostname() resolves to the container's address
# on the docker network, which is the path nginx actually uses.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,socket; socket.create_connection((socket.gethostname(), int(os.getenv('MCP_PORT','8000'))), timeout=3).close()" || exit 1

CMD ["python", "aegis_mcp_server.py"]
