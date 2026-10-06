#!/usr/bin/env python3
"""Provision a new member's agent on Discourse's `user_created` webhook.

A latency improvement: the scheduled `qupa-provision --all` is the backstop for
a missed delivery. The payload is ignored -- the listener always runs `--all`,
which surveys the forum itself -- and the signature is checked before anything
else. docs/05-forum-config.md 5.3.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger("provision-hook")

LISTEN_HOST = os.environ.get("HOOK_HOST", "172.17.0.1")
LISTEN_PORT = int(os.environ.get("HOOK_PORT", "8091"))
SECRET = os.environ.get("HOOK_SECRET", "")
COMMAND = os.environ.get("HOOK_COMMAND", "/usr/local/sbin/qupa-provision")

# One run picks up everybody, so a burst of events starts one.
DEBOUNCE_SECONDS = int(os.environ.get("HOOK_DEBOUNCE", "10"))

# `user_created` fires when an invite is redeemed; `user_approved` only with approval on.
ACTED_ON = {"user_created", "user_approved"}

_lock = threading.Lock()
_last_run = 0.0


def provision() -> None:
    """Run the provisioning, at most once per debounce window."""
    global _last_run
    with _lock:
        if time.time() - _last_run < DEBOUNCE_SECONDS:
            LOG.info("skipped: a run started less than %ds ago", DEBOUNCE_SECONDS)
            return
        _last_run = time.time()

    LOG.info("running %s --all", COMMAND)
    result = subprocess.run([COMMAND, "--all"], capture_output=True, text=True, timeout=300)
    for line in (result.stdout or "").splitlines():
        if line.startswith("==") or line.strip().startswith(("created", "sent", "added", "set")):
            LOG.info("  %s", line.strip())
    if result.returncode:
        LOG.error("provisioning exited %d: %s", result.returncode, (result.stderr or "")[-400:])


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: str = "") -> None:
        payload = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's interface
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""

        if not SECRET:
            LOG.error("refusing: HOOK_SECRET is not set, so nothing can be verified")
            self._send(503, "not configured")
            return

        presented = self.headers.get("X-Discourse-Event-Signature", "")
        expected = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(presented, expected):
            LOG.warning("refusing: signature did not verify")
            self._send(403, "bad signature")
            return

        event = self.headers.get("X-Discourse-Event", "")
        if event == "ping":
            LOG.info("ping accepted")
            self._send(200, "pong")
            return

        if event not in ACTED_ON:
            LOG.info("ignoring %s", event or "an event with no name")
            self._send(200, "ignored")
            return

        # Answered first: Discourse times out and retries a slow delivery.
        self._send(200, "accepted")

        try:
            who = (json.loads(raw).get("user") or {}).get("username")
        except (json.JSONDecodeError, AttributeError):
            who = None
        LOG.info("%s for %s", event, who or "somebody the payload did not name")

        threading.Thread(target=provision, daemon=True).start()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's interface
        if self.path == "/health":
            self._send(200, "ok")
            return
        self._send(404, "not found")

    def log_message(self, fmt: str, *args) -> None:
        LOG.debug("%s - %s", self.address_string(), fmt % args)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    if not SECRET:
        print("ERROR: set HOOK_SECRET to the webhook's secret.", file=sys.stderr)
        return 2

    httpd = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    LOG.info("listening on %s:%d", LISTEN_HOST, LISTEN_PORT)
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
