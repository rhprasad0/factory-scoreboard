#!/usr/bin/env bash
# Idempotent setup for the local factory. Run from anywhere.
#   ./setup.sh            CA, state dirs, agent image, proxy + lgtm
#   sudo ./setup.sh tetragon   install Tetragon and its config (needs root)
set -euo pipefail
cd "$(dirname "$0")"

TETRAGON_VERSION=v1.7.1

if [ "${1:-}" = "tetragon" ]; then
  [ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
  if ! command -v tetragon >/dev/null; then
    tmp=$(mktemp -d)
    curl -fsSLo "$tmp/t.tgz" "https://github.com/cilium/tetragon/releases/download/$TETRAGON_VERSION/tetragon-$TETRAGON_VERSION-amd64.tar.gz"
    curl -fsSLo "$tmp/t.sha" "https://github.com/cilium/tetragon/releases/download/$TETRAGON_VERSION/tetragon-$TETRAGON_VERSION-amd64.tar.gz.sha256sum"
    (cd "$tmp" && echo "$(cut -d' ' -f1 t.sha)  t.tgz" | sha256sum -c -)
    tar -xzf "$tmp/t.tgz" -C "$tmp"
    (cd "$tmp/tetragon-$TETRAGON_VERSION-amd64" && ./install.sh)
    rm -rf "$tmp"
  fi
  install -m 0644 tetragon/conf.d/* /etc/tetragon/tetragon.conf.d/
  install -m 0644 tetragon/policies/*.yaml /etc/tetragon/tetragon.tp.d/
  # The export path lives under this checkout, so tell Tetragon where it is.
  echo "$PWD/state/tetragon/tetragon.log" > /etc/tetragon/tetragon.conf.d/export-filename
  systemctl restart tetragon
  echo "tetragon: $(systemctl is-active tetragon)"
  exit 0
fi

[ -f .env ] || { echo "missing sandbox/.env (copy .env.example and add the API key)" >&2; exit 1; }
docker compose version >/dev/null
mkdir -p certs state/flows state/tetragon state/jobs

if [ ! -f certs/mitmproxy-ca-cert.pem ]; then
  docker run --rm --user 0:0 -v "$PWD/certs:/certs" mitmproxy/mitmproxy:12.2.3 \
    sh -c 'timeout 5 mitmdump --set confdir=/certs >/dev/null 2>&1; true'
fi
test -f certs/mitmproxy-ca-cert.pem

docker build -q -t factory-agent:dev -f agent/Dockerfile . >/dev/null
docker compose up -d proxy lgtm
echo "ok: grafana at http://localhost:3300"
