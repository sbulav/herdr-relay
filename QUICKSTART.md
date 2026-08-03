# Quick Start

Get mobile notifications + approval for your herdr agents in 60 seconds. This is
the throwaway path — for a host you intend to keep, see
[`docs/deployment.md`](docs/deployment.md).

## 1. Start the relay

```bash
git clone https://github.com/sbulav/herdr-relay
cd herdr-relay
export HERDR_RELAY_TOKEN="$(openssl rand -hex 16)"   # required — no default
uv run relay/herdr-relay.py
```

## 2. Expose it

```bash
# Cloudflare quick tunnel (free, instant, disposable):
cloudflared tunnel --url http://localhost:8375
# → gives you https://something.trycloudflare.com
```

## 3. Install the plugin (on any machine with herdr)

```bash
herdr plugin install dcolinmorgan/herdr-push
export HERDR_RELAY="https://your-tunnel.trycloudflare.com"
launchctl setenv HERDR_RELAY "$HERDR_RELAY"
herdr server reload-config
```

## 4. Monitor

**Web app** (phone):
Open [herdr-remote.pages.dev](https://herdr-remote.pages.dev), tap ⚙, paste your
tunnel URL and the token from step 1.

**Telegram bot**:
```bash
export HERDR_TG_TOKEN="your-token" HERDR_TG_CHAT_ID="your-id"
uv run relay/herdr_telegram.py
```

## 5. Test

```bash
herdr plugin action invoke herdr.push test
```

You should see a test agent appear on your dashboard.
