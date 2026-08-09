# Perseus Ledger — verifiable provenance for autonomous systems.
# One-command self-hosted deploy: docker run -p 8420:8420 ledger
FROM python:3.12-slim

LABEL org.opencontainers.image.title="Perseus Ledger"
LABEL org.opencontainers.image.description="Runtime-neutral, self-hosted, hash-chained event provenance for autonomous systems."
LABEL org.opencontainers.image.source="https://github.com/Perseus-Computing-LLC/ledger"
LABEL org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    LEDGER_HOME=/data \
    LEDGER_PORT=8420

WORKDIR /app

# Install the package (with Stripe + PDF extras) from source.
COPY pyproject.toml README.md LICENSE ./
COPY ledger_agent ./ledger_agent
RUN pip install --no-cache-dir ".[all]"

# Run as a non-root user; /data (config + SQLite) is owned by it.
RUN useradd --system --create-home --uid 10001 ledger \
 && mkdir -p /data && chown -R ledger:ledger /data /app
USER ledger

# State (config + SQLite) persists in a mounted volume.
VOLUME ["/data"]
EXPOSE 8420

# Default is DEMO mode (throwaway sample data, no auth) so
# `docker run -p 8420:8420 ledger-agent` shows value instantly; it passes
# --allow-insecure because it is a disposable demo. For a REAL deploy, override
# the command AND configure auth — the server refuses to bind 0.0.0.0 with auth
# off (see docs/deploy-hardening.md):
#   docker run -p 8420:8420 -v ledger:/data \
#     -e LEDGER_AUTH_ENABLED=1 -e LEDGER_GOOGLE_CLIENT_ID=... \
#     -e LEDGER_GOOGLE_CLIENT_SECRET=... -e LEDGER_BASE_URL=https://host \
#     ledger-agent serve --host 0.0.0.0
HEALTHCHECK --interval=30s --timeout=4s --start-period=5s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8420/healthz',timeout=3).status==200 else 1)"

ENTRYPOINT ["ledger"]
CMD ["serve", "--demo", "--host", "0.0.0.0", "--allow-insecure"]
