"""One-time script to register the Telegram webhook pointing to your Cloudflare Worker."""

import json
import os
import secrets
import sys
import urllib.request

from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token:
        print("TELEGRAM_BOT_TOKEN is not set in .env")
        sys.exit(1)

    webhook_secret = os.environ.get("WEBHOOK_SECRET", "").strip()
    if not webhook_secret:
        webhook_secret = secrets.token_hex(32)
        print(f"Generated WEBHOOK_SECRET: {webhook_secret}")
        print("Add this to your .env and Cloudflare Worker secrets.")

    worker_url = input(
        "Enter your Cloudflare Worker URL\n"
        "(e.g. https://gym-buddy-webhook.<your-subdomain>.workers.dev): "
    ).strip().rstrip("/")

    if not worker_url:
        print("Worker URL is required.")
        sys.exit(1)

    payload = {
        "url": worker_url,
        "allowed_updates": ["poll_answer"],
        "secret_token": webhook_secret,
    }

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token}/setWebhook",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req)
    result = json.loads(resp.read().decode())
    print(json.dumps(result, indent=2))

    if result.get("ok"):
        print("\nWebhook registered successfully!")
        print(f"URL: {worker_url}")
    else:
        print("\nWebhook registration failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
