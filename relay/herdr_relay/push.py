"""Web Push for the browser client.

Browser-only by design: herdr-mobile monitors the WebSocket in a foreground
service and never subscribes, so this module, the HERDR_VAPID_* variables, and
the push_subscribe/push_unsubscribe handlers exist for web/ alone. web/ is a
supported client (herdr-mobile#37 superseded the plan to retire it, #14), so
none of it is transitional.
"""
import json
import os

from . import config
from .config import log

push_subscriptions = []  # list of PushSubscription dicts


def _load_push_subs():
    global push_subscriptions
    if os.path.isfile(config.PUSH_SUBS_FILE):
        try:
            with open(config.PUSH_SUBS_FILE) as f:
                push_subscriptions = json.load(f)
        except Exception:
            push_subscriptions = []


def _save_push_subs():
    with open(config.PUSH_SUBS_FILE, "w") as f:
        json.dump(push_subscriptions, f)


def subscribe(sub) -> bool:
    """Register a browser subscription. True when it was new."""
    if not sub or sub in push_subscriptions:
        return False
    push_subscriptions.append(sub)
    _save_push_subs()
    return True


def unsubscribe(sub) -> bool:
    """Forget a browser subscription. True when it was registered."""
    if not sub or sub not in push_subscriptions:
        return False
    push_subscriptions.remove(sub)
    _save_push_subs()
    return True


async def send_web_push(title: str, body: str, url: str = "/", clear: bool = False):
    """Send push notification to all registered subscriptions.

    Uses collapse topic + TTL so offline devices get only the latest.
    If clear=True, sends a clear instruction instead of showing a notification.
    """
    if not config.VAPID_PUBLIC_KEY or not config.VAPID_PRIVATE_KEY:
        return
    try:
        from pywebpush import webpush
    except ImportError:
        log.warning("pywebpush not installed, skipping push")
        return
    if clear:
        payload = json.dumps({"type": "clear", "tag": "herdr-blocked"})
    else:
        payload = json.dumps({"title": title, "body": body, "url": url})
    headers = {"Topic": "herdr-herd", "TTL": "21600"}  # 6h TTL, collapse key
    dead = []
    for i, sub in enumerate(push_subscriptions):
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=config.VAPID_PRIVATE_KEY,
                vapid_claims={"sub": config.VAPID_SUBJECT},
                headers=headers,
            )
        except Exception as e:
            log.warning("Push failed for sub %d: %s", i, e)
            if "410" in str(e) or "404" in str(e):
                dead.append(i)
    if dead:
        for i in reversed(dead):
            push_subscriptions.pop(i)
        _save_push_subs()


_load_push_subs()
