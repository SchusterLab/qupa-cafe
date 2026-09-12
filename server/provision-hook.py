#!/usr/bin/env python3
"""Provision a new member's agent the moment they join, rather than on the next poll.

A scheduled run every fifteen minutes is a fine backstop and a poor welcome: a
researcher redeems an invite, reads a message telling them their credential is
coming, and then waits. This listens for Discourse's `user_created` webhook and
runs the provisioning immediately.

The webhook is a latency improvement and nothing more. `45-provision-agent.py
--all` still runs on its schedule, because a webhook can be missed -- the
listener restarting, the container being rebuilt, a delivery that fails and is
not retried -- and a missed one is invisible from the forum's side. Both paths
do the same idempotent thing, so the poll simply finds nothing to do.

Two decisions worth knowing about.

**Nothing from the request reaches a command line.** The payload names the user
who was created, and that name is deliberately ignored: the listener runs
`--all`, which surveys the forum itself. So a forged or malformed payload can
cause an extra no-op run and nothing else, and there is no argument to inject
into.

**The signature is checked before anything else happens.** Discourse signs the
raw body with the webhook's secret, and a request that does not verify is
answered 403 without being parsed.

Run it as a service, on the docker bridge address so the container can reach it
and the network cannot. See docs/05-forum-config.md.
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
COMMAND = os.environ.get("HOOK_COMMAND", "/root/run-prov.sh")

# A burst of events -- an invite redeemed while another is being approved --
# should not start a run per event. One run picks up everybody anyway.
DEBOUNCE_SECONDS = int(os.environ.get("HOOK_DEBOUNCE", "10"))

# Events worth acting on. `user_created` is the one that fires when an invite is
# redeemed; `user_approved` matters only on a forum that has approval on, and is
# harmless here because the run is idempotent.
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

        # Answered before the work starts, because Discourse times a delivery
        # out and retries, and a retry would only start the same run again.
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
