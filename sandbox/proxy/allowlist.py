"""mitmproxy addon: the sandbox's only way out.

- Hosts not in allowlist.txt get a 403, both at CONNECT and per request.
- Requests to api.anthropic.com lose whatever credential the client sent and get the
  real key from the proxy's own environment, so the agent never holds it.
- Every flow is appended to /state/flows/flows.jsonl, tagged with the current job id.
"""

import json
import os
import time
from pathlib import Path

from mitmproxy import http

HERE = Path(__file__).parent
ALLOWED = {
    line.strip()
    for line in (HERE / "allowlist.txt").read_text().splitlines()
    if line.strip() and not line.startswith("#")
}
API_HOST = "api.anthropic.com"
OTLP_PORT = 4318  # the reverse listener that forwards telemetry to lgtm
JOB_FILE = Path("/state/current-job")
LOG = Path("/state/flows/flows.jsonl")
LOG.parent.mkdir(parents=True, exist_ok=True)


def _job() -> str:
    try:
        return JOB_FILE.read_text().strip() or "none"
    except OSError:
        return "none"


def _log(flow: http.HTTPFlow, allowed: bool, kind: str) -> None:
    req = flow.request
    record = {
        "time_ms": int(time.time() * 1000),
        "job_id": _job(),
        "kind": kind,
        "client": flow.client_conn.peername[0] if flow.client_conn.peername else None,
        "host": req.pretty_host,
        "method": req.method,
        "path": req.path if kind != "connect" else None,
        "status": flow.response.status_code if flow.response else None,
        "bytes_out": len(req.raw_content or b""),
        "bytes_in": len(flow.response.raw_content or b"") if flow.response else 0,
        "allowed": allowed,
    }
    with LOG.open("a") as f:
        f.write(json.dumps(record) + "\n")


def _is_telemetry(flow: http.HTTPFlow) -> bool:
    return flow.client_conn.sockname is not None and flow.client_conn.sockname[1] == OTLP_PORT


def _deny(flow: http.HTTPFlow) -> None:
    flow.response = http.Response.make(403, b"blocked by factory proxy\n", {"content-type": "text/plain"})


def http_connect(flow: http.HTTPFlow) -> None:
    if flow.request.pretty_host not in ALLOWED:
        _deny(flow)
        _log(flow, allowed=False, kind="connect")


def request(flow: http.HTTPFlow) -> None:
    if _is_telemetry(flow):
        return
    if flow.request.pretty_host not in ALLOWED:
        _deny(flow)
        _log(flow, allowed=False, kind="request")
        return
    if flow.request.pretty_host == API_HOST:
        for header in ("authorization", "x-api-key"):
            flow.request.headers.pop(header, None)
        flow.request.headers["x-api-key"] = os.environ["ANTHROPIC_API_KEY"]


def response(flow: http.HTTPFlow) -> None:
    if flow.response and flow.response.status_code == 403 and flow.response.content == b"blocked by factory proxy\n":
        return  # already logged by the hook that blocked it
    _log(flow, allowed=True, kind="telemetry" if _is_telemetry(flow) else "request")
