# The worker and build sandbox. Build context is sandbox/ (it needs certs/).
FROM rust:1.99.1-slim-trixie

ARG CLAUDE_CODE_VERSION=2.1.322
ARG CARGO_LAMBDA_VERSION=1.9.2

RUN apt-get update -qq \
 && apt-get install -y --no-install-recommends ca-certificates curl git jq \
 && rm -rf /var/lib/apt/lists/* \
 && rustup component add rustfmt clippy

RUN curl -fsSLo /tmp/cl.tgz "https://github.com/cargo-lambda/cargo-lambda/releases/download/v${CARGO_LAMBDA_VERSION}/cargo-lambda-v${CARGO_LAMBDA_VERSION}.x86_64-unknown-linux-musl.tar.gz" \
 && curl -fsSLo /tmp/cl.sha "https://github.com/cargo-lambda/cargo-lambda/releases/download/v${CARGO_LAMBDA_VERSION}/cargo-lambda-v${CARGO_LAMBDA_VERSION}.x86_64-unknown-linux-musl.tar.gz.sha256" \
 && echo "$(cut -d' ' -f1 /tmp/cl.sha)  /tmp/cl.tgz" | sha256sum -c - \
 && tar -xzf /tmp/cl.tgz -C /usr/local/bin \
 && rm /tmp/cl.*

# The proxy's CA, so Claude Code and cargo trust it through the OS store.
COPY certs/mitmproxy-ca-cert.pem /usr/local/share/ca-certificates/factory-mitm.crt
RUN update-ca-certificates

RUN useradd --create-home --uid 1000 agent \
 && mkdir -p /work /home/agent/.cargo/registry \
 && chown -R agent:agent /work /home/agent/.cargo
COPY agent/run-agent.sh agent/export-bundle.sh /usr/local/bin/

USER agent
RUN curl -fsSL https://claude.ai/install.sh | bash -s "${CLAUDE_CODE_VERSION}"

ENV PATH="/home/agent/.local/bin:${PATH}" \
    CARGO_HOME=/home/agent/.cargo \
    HTTPS_PROXY=http://proxy:8080 \
    HTTP_PROXY=http://proxy:8080 \
    NO_PROXY=localhost,127.0.0.1,proxy \
    https_proxy=http://proxy:8080 \
    http_proxy=http://proxy:8080 \
    no_proxy=localhost,127.0.0.1,proxy \
    DISABLE_AUTOUPDATER=1
WORKDIR /work
