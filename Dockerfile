# Perseus Ledger — verifiable provenance for autonomous systems.
# One-command self-hosted deploy: docker run -p 8420:8420 ledger
FROM python:3.12-slim

LABEL org.opencontainers.image.title="Perseus Ledger"
LABEL org.opencontainers.image.description="Runtime-neutral, self-hosted, hash-chained event provenance for autonomous systems."
LABEL org.opencontainers.image.source="https://github.com/Perseus-Computing-LLC/ledger"
LABEL org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PLUTUS_HOME=/data \
    PLUTUS_PORT=8420

WORKDIR /app

# Install the package (with Stripe + PDF extras) from source.
COPY pyproject.toml README.md LICENSE ./
COPY plutus_agent ./plutus_agent
RUN pip install --no-cache-dir ".[all]"

# Run as a non-root user; /data (config + SQLite) is owned by it.
RUN useradd --system --create-home --uid 10001 plutus \
 && mkdir -p /data && chown -R plutus:plutus /data /app
USER plutus

# State (config + SQLite) persists in a mounted volume.
VOLUME ["/data"]
EXPOSE 8420

# Default is DEMO mode (throwaway sample data, no auth) so
# `docker run -p 8420:8420 plutus-agent` shows value instantly; it passes
# --allow-insecure because it is a disposable demo. For a REAL deploy, override
# the command AND configure auth — the server refuses to bind 0.0.0.0 with auth
# off (see docs/deploy-hardening.md):
#   docker run -p 8420:8420 -v plutus:/data \
#     -e PLUTUS_AUTH_ENABLED=1 -e PLUTUS_GOOGLE_CLIENT_ID=... \
#     -e PLUTUS_GOOGLE_CLIENT_SECRET=... -e PLUTUS_BASE_URL=https://host \
#     plutus-agent serve --host 0.0.0.0
HEALTHCHECK --interval=30s --timeout=4s --start-period=5s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8420/healthz',timeout=3).status==200 else 1)"

ENTRYPOINT ["plutus"]
CMD ["serve", "--demo", "--host", "0.0.0.0", "--allow-insecure"]
