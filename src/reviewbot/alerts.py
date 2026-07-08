"""Negative-review alerts — ping the moment a NEW negative review appears.

OFF by default: nothing is sent unless a channel is configured via env —
  - ALERT_WEBHOOK_URL   a Slack OR Discord "incoming webhook" (one URL), or
  - SMTP_HOST + ALERT_EMAIL_TO (+ SMTP_USER/SMTP_PASS/ALERT_EMAIL_FROM) for email.

Anti-storm: the first time we ever alert for a brand, its negatives from that
pass are recorded as a baseline (a single "now watching" heads-up, no blast).
Only negatives seen AFTER that trigger individual alerts, deduped by review id,
capped per pass. State (alerted ids, brands seen) persists in ALERT_STATE_PATH.
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import threading
from email.mime.text import MIMEText

import requests

log = logging.getLogger(__name__)

STATE_PATH = os.environ.get("ALERT_STATE_PATH", "/app/state/alert_state.json")
_LOCK = threading.Lock()


def _int_env(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except ValueError:
        log.warning("Ignoring invalid %s=%r; using %d", name, value, default)
        return default


MAX_PER_PASS = _int_env("ALERT_MAX_PER_PASS", 5)


def _channels() -> list[str]:
    ch = []
    if os.environ.get("ALERT_WEBHOOK_URL"):
        ch.append("webhook")
    if os.environ.get("ALERT_EMAIL_TO") and os.environ.get("SMTP_HOST"):
        ch.append("email")
    return ch


def enabled() -> bool:
    return bool(_channels())


def _load_state() -> dict:
    try:
        with open(STATE_PATH) as fh:
            s = json.load(fh)
    except Exception:  # noqa: BLE001
        s = {}
    s.setdefault("alerted", [])
    s.setdefault("brands_seen", [])
    s.setdefault("sent_count", 0)
    return s


def _save_state(s: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(s, fh)
    os.replace(tmp, STATE_PATH)


def _send_webhook(text: str) -> bool:
    url = os.environ.get("ALERT_WEBHOOK_URL")
    if not url:
        return False
    try:
        # {"text": …} is Slack's field, {"content": …} is Discord's — send both so
        # one URL works for either service.
        r = requests.post(url, json={"text": text, "content": text}, timeout=10)
        r.raise_for_status()
        return True
    except requests.RequestException:
        log.exception("alert webhook failed")
        return False


def _send_email(subject: str, text: str) -> bool:
    to = os.environ.get("ALERT_EMAIL_TO")
    host = os.environ.get("SMTP_HOST")
    if not (to and host):
        return False
    try:
        msg = MIMEText(text)
        msg["Subject"] = subject
        msg["From"] = os.environ.get("ALERT_EMAIL_FROM", "reviewbot@localhost")
        msg["To"] = to
        with smtplib.SMTP(host, _int_env("SMTP_PORT", 587), timeout=15) as smtp:
            smtp.starttls()
            user, pw = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS")
            if user and pw:
                smtp.login(user, pw)
            smtp.send_message(msg)
        return True
    except Exception:  # noqa: BLE001
        log.exception("alert email failed")
        return False


def _send(text: str, subject: str = "ReviewBay alert") -> bool:
    ok = False
    if os.environ.get("ALERT_WEBHOOK_URL"):
        ok = _send_webhook(text) or ok
    if os.environ.get("ALERT_EMAIL_TO") and os.environ.get("SMTP_HOST"):
        ok = _send_email(subject, text) or ok
    return ok


def _fmt(r: dict) -> str:
    stars = f"{r.get('rating')}/5 " if r.get("rating") is not None else ""
    excerpt = (r.get("text") or "").strip().replace("\n", " ")
    if len(excerpt) > 200:
        excerpt = excerpt[:200] + "..."
    return (
        f"New negative review, {r.get('brand')}\n"
        f"{stars}on {r.get('source')}\n"
        f'"{excerpt}"\n'
        f"{r.get('source_url')}"
    )


def notify_new_negatives(reviews: list[dict]) -> int:
    """Alert on brand-new negative reviews. Returns how many alerts were sent."""
    if not enabled():
        return 0
    negs = [r for r in reviews if r.get("sentiment") == "negative" and r.get("id")]
    if not negs:
        return 0

    sent = 0
    with _LOCK:
        state = _load_state()
        alerted = set(state["alerted"])
        seen = set(state["brands_seen"])

        by_brand: dict[str, list] = {}
        for r in negs:
            by_brand.setdefault(r.get("brand") or "?", []).append(r)

        for brand, items in by_brand.items():
            fresh = [r for r in items if r["id"] not in alerted]
            if not fresh:
                continue
            if brand not in seen:
                # First time for this brand: baseline, one heads-up, no blast.
                for r in fresh:
                    alerted.add(r["id"])
                seen.add(brand)
                if _send(
                    f"Now watching {brand}, {len(fresh)} negative review(s) at setup. "
                    "You'll be pinged on new ones from here.",
                    subject=f"ReviewBay now watching {brand}",
                ):
                    sent += 1
                continue

            for r in fresh[:MAX_PER_PASS]:
                if _send(_fmt(r), subject=f"Negative review, {brand}"):
                    sent += 1
                alerted.add(r["id"])
            if len(fresh) > MAX_PER_PASS:
                extra = len(fresh) - MAX_PER_PASS
                if _send(f"And {extra} more new negative review(s) for {brand}."):
                    sent += 1
                for r in fresh[MAX_PER_PASS:]:
                    alerted.add(r["id"])

        state["alerted"] = list(alerted)
        state["brands_seen"] = list(seen)
        state["sent_count"] = state.get("sent_count", 0) + sent
        _save_state(state)
    return sent


def status() -> dict:
    return {
        "enabled": enabled(),
        "channels": _channels(),
        "sent_count": _load_state().get("sent_count", 0),
    }


def send_test() -> bool:
    return _send(
        "ReviewBay test alert. Your alerts are wired up correctly.",
        subject="ReviewBay test alert",
    )
