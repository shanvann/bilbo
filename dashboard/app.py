#!/usr/bin/env python3
"""Dashboard frontend — static + PWA + reverse proxy to control-api.

The dashboard container has no bilbo imports and no business logic. It
serves the static HTML/JS/CSS and the PWA service worker; everything
under /api/* is reverse-proxied to control-api:5556/api/v1/*. The proxy
forwards method, query string, body, and (most) headers; the conditional
streaming for /api/frame and /api/recap/video works because httpx exposes
the response body as a stream.
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx
from flask import Flask, Response, request, send_from_directory

CONTROL_API = os.environ.get("BILBO_CONTROL_API", "http://control-api:5556")

# Cloudflare Access service-token injection: the installed PWA loses its
# Access SSO cookie on cold launches and background fetches. The service
# worker is patched at serve time with the bot's service token so requests
# from the worker carry the right headers and skip the interactive SSO
# flow. Falls back to empty values if the env vars aren't set (dev mode).
_CF_ACCESS = {
    "id": os.environ.get("CF_ACCESS_CLIENT_ID", ""),
    "secret": os.environ.get("CF_ACCESS_CLIENT_SECRET", ""),
}

app = Flask(__name__, static_folder="static")


# ---------------------------------------------------------------------------
# Static + PWA
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/sw.js")
def service_worker():
    # Served from /sw.js (not /static/sw.js) so its scope covers the whole origin.
    sw_path = Path(app.static_folder) / "sw.js"
    body = sw_path.read_text()
    body = body.replace("__CF_ACCESS_CLIENT_ID__", _CF_ACCESS["id"])
    body = body.replace("__CF_ACCESS_CLIENT_SECRET__", _CF_ACCESS["secret"])
    response = app.response_class(body, mimetype="application/javascript")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory("static", "manifest.webmanifest")


# ---------------------------------------------------------------------------
# Reverse proxy /api/* → control-api /api/v1/*
# ---------------------------------------------------------------------------

# Hop-by-hop headers per RFC 7230 — must not be forwarded across proxies.
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
}

# Frame and recap responses are large/binary and the <video> element issues
# Range requests; streaming both directions keeps memory flat and preserves
# 206 Partial Content semantics.
_STREAMING_PATHS = ("frame", "recap/video")


@app.route("/api/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def proxy(subpath: str):
    url = f"{CONTROL_API}/api/v1/{subpath}"
    headers = {k: v for k, v in request.headers if k.lower() not in _HOP_BY_HOP}
    streaming = any(subpath == p or subpath.startswith(p + "/") or subpath.endswith("/" + p) for p in _STREAMING_PATHS)

    if streaming:
        client = httpx.Client(timeout=60.0)
        upstream = client.stream(
            request.method, url,
            params=request.args,
            content=request.get_data(),
            headers=headers,
        )
        ctx = upstream.__enter__()
        resp_headers = [(k, v) for k, v in ctx.headers.items() if k.lower() not in _HOP_BY_HOP]
        def gen():
            try:
                for chunk in ctx.iter_bytes():
                    yield chunk
            finally:
                upstream.__exit__(None, None, None)
                client.close()
        return Response(gen(), status=ctx.status_code, headers=resp_headers)

    upstream = httpx.request(
        request.method, url,
        params=request.args,
        content=request.get_data(),
        headers=headers,
        timeout=60.0,
    )
    resp_headers = [(k, v) for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP]
    return Response(upstream.content, status=upstream.status_code, headers=resp_headers)


# ---------------------------------------------------------------------------
# Entry point (gunicorn loads `app` directly; this is for local dev)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5555, debug=False)
