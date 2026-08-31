"""
Ops-facing alerting -- a real Slack-compatible incoming-webhook call, fired
on the events an on-call human actually needs to know about immediately
rather than discover later in a dashboard: a systemic incident detected, a
high-stakes action paused for human approval, or an audit-chain integrity
check failing. None of this is simulated -- when SLACK_WEBHOOK_URL is set,
this makes a real HTTP POST to a real Slack (or any Slack-compatible
webhook, e.g. Discord/Mattermost's Slack-compatible endpoints) channel.

Same optional-integration philosophy as core/llm_client.py: nothing here
ever breaks the pipeline. No SLACK_WEBHOOK_URL configured -> every call is a
no-op that returns False, instantly, no network attempted -- the expected,
tested state for most deployments of this project, not an error condition.
A configured-but-unreachable webhook fails the same way: caught, returned
False, never raised into the caller.
"""

from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from typing import Optional

SLACK_WEBHOOK_URL_ENV = "SLACK_WEBHOOK_URL"


def send_alert(event_type: str, message: str, context: Optional[dict] = None, timeout: float = 5.0) -> bool:
    """Fire a Slack-compatible incoming-webhook message. Returns True only on
    a genuine 2xx response from the webhook; False for "not configured",
    "network/HTTP error", or any other failure -- callers that want to know
    WHY can inspect the return value's falsiness, but should never need a
    try/except around this call, by design."""
    url = os.environ.get(SLACK_WEBHOOK_URL_ENV)
    if not url:
        return False

    text = f"*[{event_type}]* {message}"
    if context:
        text += "\n" + "\n".join(f"• {k}: {v}" for k, v in context.items())

    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:  # noqa: BLE001 -- deliberately broad, must never crash a caller over an alert
        return False
