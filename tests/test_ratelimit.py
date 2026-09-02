import asyncio
import json
import unittest
from unittest.mock import patch

from relay import herdr_relay


class Clock:
    """A monotonic clock the test advances by hand.

    Rate limiting is entirely about elapsed time, and a test that sleeps to
    observe a refill is a test that fails on a loaded machine.
    """

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TokenBucketTests(unittest.TestCase):
    def test_a_burst_is_spent_then_refused(self):
        clock = Clock()
        bucket = herdr_relay.ratelimit.TokenBucket(3, 1, clock)
        self.assertEqual([True, True, True, False], [bucket.take() for _ in range(4)])

    def test_the_bucket_refills_at_the_configured_rate(self):
        clock = Clock()
        bucket = herdr_relay.ratelimit.TokenBucket(2, 2, clock)
        self.assertEqual([True, True, False], [bucket.take() for _ in range(3)])
        clock.advance(0.4)  # 0.8 tokens: still short of one
        self.assertFalse(bucket.take())
        clock.advance(0.1)  # now past a whole token
        self.assertTrue(bucket.take())

    def test_refill_never_exceeds_the_burst(self):
        """An idle connection banks one burst, not an hour's worth of allowance."""
        clock = Clock()
        bucket = herdr_relay.ratelimit.TokenBucket(2, 5, clock)
        clock.advance(3600)
        self.assertEqual([True, True, False], [bucket.take() for _ in range(3)])

    def test_a_zero_burst_disables_the_tier(self):
        bucket = herdr_relay.ratelimit.TokenBucket(0, 0, Clock())
        self.assertTrue(bucket.disabled)
        self.assertEqual([True] * 50, [bucket.take() for _ in range(50)])

    def test_a_clock_that_goes_backwards_does_not_strand_the_bucket(self):
        """Credit nothing for negative elapsed time, but keep taking new readings.

        Leaving `_updated` in the future would make every subsequent refill
        negative too, so the bucket would never recover.
        """
        clock = Clock()
        bucket = herdr_relay.ratelimit.TokenBucket(1, 1, clock)
        self.assertTrue(bucket.take())
        clock.advance(-30)
        self.assertFalse(bucket.take())
        clock.advance(31)
        self.assertTrue(bucket.take())


class ConnectionLimitsTests(unittest.TestCase):
    def test_the_tiers_are_metered_independently(self):
        """Exhausting terminal input must not stop a client reading a pane.

        They are separate buckets because the point is to bound a loop, not to
        strand a session that typed too fast into one that cannot see why.
        """
        clock = Clock()
        with (
            patch.object(herdr_relay.config, "RATE_INPUT_BURST", 1),
            patch.object(herdr_relay.config, "RATE_INPUT_PER_SECOND", 0),
            patch.object(herdr_relay.config, "RATE_HOST_BURST", 5),
            patch.object(herdr_relay.config, "RATE_HOST_PER_SECOND", 0),
        ):
            limits = herdr_relay.ratelimit.ConnectionLimits(clock)
        self.assertTrue(limits.allows("send_keys"))
        self.assertFalse(limits.allows("send_text"))
        self.assertTrue(limits.allows("read_pane"))

    def test_unmetered_commands_are_never_limited(self):
        """Nothing here shells out, so nothing here has a host to protect."""
        limits = herdr_relay.ratelimit.ConnectionLimits(Clock())
        for msg_type in ("agent_event", "unsubscribe_pane", "push_subscribe", "push_unsubscribe", None):
            self.assertIsNone(limits.bucket_for(msg_type), msg_type)
            self.assertEqual([True] * 100, [limits.allows(msg_type) for _ in range(100)])

    def test_every_project_command_is_metered(self):
        """Drift guard: `project_browse` walks a remote tree over ssh.

        `HOST_COMMANDS` derives the project names from the handler table, so this
        fails only if that derivation is replaced with a hand-written list.
        """
        limits = herdr_relay.ratelimit.ConnectionLimits(Clock())
        for msg_type in herdr_relay.projects.COMMANDS:
            self.assertIs(limits.host, limits.bucket_for(msg_type), msg_type)

    def test_every_terminal_write_is_metered_by_the_strict_tier(self):
        limits = herdr_relay.ratelimit.ConnectionLimits(Clock())
        for msg_type in ("respond", "respond_dialog", "send_keys", "send_text", "send_prompt"):
            self.assertIs(limits.input, limits.bucket_for(msg_type), msg_type)


class RejectionDialectTests(unittest.TestCase):
    def test_a_typed_command_is_refused_with_a_code(self):
        frame = herdr_relay.ratelimit.rejection("start_session", "req-7")
        self.assertEqual("command_error", frame["type"])
        self.assertEqual("RATE_LIMITED", frame["code"])
        self.assertEqual("req-7", frame["request_id"])

    def test_send_prompt_is_refused_with_a_correlated_code(self):
        frame = herdr_relay.ratelimit.rejection("send_prompt", "req-prompt")
        self.assertEqual({
            "type": "command_error",
            "request_id": "req-prompt",
            "code": "RATE_LIMITED",
            "message": "Too many requests, slow down",
        }, frame)

    def test_a_pane_command_is_refused_in_its_own_dialect(self):
        """`send_keys` has no `request_id` and its client parses `error`.

        Answering it with `command_error` would be a frame that client drops.
        """
        frame = herdr_relay.ratelimit.rejection("send_keys", None)
        self.assertEqual({"type": "error", "message": "rate limited, slow down"}, frame)


class HandleClientRateLimitTests(unittest.TestCase):
    """The limit as a client experiences it: through the real dispatch loop."""

    def _drive(self, messages):
        sent = []
        audited = []

        class FakeWS:
            remote_address = ("203.0.113.11", 54330)
            # A UA the device sniffing actually recognises: the audit trail naming
            # the device is half of why a rejection is audited at all.
            request = type("Request", (), {"headers": {"User-Agent": "okhttp/4.12 (Android 14; herdr-mobile)"}})()

            async def send(inner, raw):
                sent.append(json.loads(raw))

            def __aiter__(inner):
                inner._pending = iter(messages)
                return inner

            async def __anext__(inner):
                try:
                    return json.dumps(next(inner._pending))
                except StopIteration:
                    raise StopAsyncIteration

        with (
            patch.dict(herdr_relay.state.pane_remote_map, {}, clear=True),
            patch.object(herdr_relay.state, "known_panes", {"pane-7"}),
            patch.object(herdr_relay.state, "known_pane_keys", {("local", "pane-7")}),
            patch.dict(herdr_relay.state.pane_hosts, {"pane-7": {"local"}}, clear=True),
            patch.object(herdr_relay.herdr, "run_herdr", return_value="") as run_herdr,
            patch.object(herdr_relay.server, "audit", lambda *args: audited.append(args)),
        ):
            asyncio.run(herdr_relay.handle_client(FakeWS()))
        return sent, audited, run_herdr

    def test_a_flood_of_keys_stops_reaching_the_host(self):
        """The bucket is what bounds the terminal, so assert on the herdr calls.

        Asserting only on the rejection frames would still pass if the relay sent
        an error and ran the command anyway.
        """
        with (
            patch.object(herdr_relay.config, "RATE_INPUT_BURST", 3),
            patch.object(herdr_relay.config, "RATE_INPUT_PER_SECOND", 0),
        ):
            sent, audited, run_herdr = self._drive(
                [{"type": "send_keys", "pane_id": "pane-7", "keys": ["Enter"]}] * 6
            )

        self.assertEqual(3, run_herdr.call_count)
        rejections = [frame for frame in sent if frame.get("message") == "rate limited, slow down"]
        self.assertEqual(3, len(rejections))
        self.assertEqual(
            [("rate_limited", "203.0.113.11", "Android", "pane-7", "type=send_keys")] * 3,
            [entry for entry in audited if entry[0] == "rate_limited"],
        )

    def test_a_replayed_request_is_not_metered(self):
        """A remembered answer reaches no host, so it cannot be the thing limited.

        `start_session` is dispatched once and the second identical `request_id`
        is served from `request_results` — with a burst of 1, a metered replay
        would come back as RATE_LIMITED instead of the original ack.
        """
        ack = {"type": "command_ack", "request_id": "req-1", "result": {}}
        with (
            patch.object(herdr_relay.config, "RATE_HOST_BURST", 1),
            patch.object(herdr_relay.config, "RATE_HOST_PER_SECOND", 0),
            patch.object(herdr_relay.lifecycle, "start_session", return_value=ack),
        ):
            sent, _, _ = self._drive([{"type": "start_session", "request_id": "req-1"}] * 2)

        self.assertEqual([ack, ack], [frame for frame in sent if frame["type"] != "server_info"])


if __name__ == "__main__":
    unittest.main()
