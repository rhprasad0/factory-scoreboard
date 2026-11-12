# The oracle grader: the agent image plus Python for the holdout's pytest + httpx scenarios.
# Separate so the worker's environment (and so the baseline) stays unchanged.
FROM factory-agent:dev

USER root
# The base image points at the sandbox proxy, which does not exist at build time.
RUN env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy sh -c '\
    apt-get update -qq \
 && apt-get install -y --no-install-recommends python3 python3-pytest python3-httpx \
 && rm -rf /var/lib/apt/lists/*'
USER agent
