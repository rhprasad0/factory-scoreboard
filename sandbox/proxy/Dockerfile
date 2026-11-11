# The addon is baked in, not bind-mounted: mitmproxy unloads a script whose file
# disappears and then runs as an open proxy (found 2026-11-11 when a git checkout
# briefly removed it). host/factory.py also preflights the proxy before every job.
FROM mitmproxy/mitmproxy:12.2.3
COPY allowlist.py allowlist.txt /addon/
