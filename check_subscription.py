"""
Checks whether the Mawaid app is subscribed to receive webhooks for the
production WABA, and re-subscribes if not. Run this whenever incoming
WhatsApp messages aren't reaching the webhook at all (no POST /webhook
logged).

Usage:
    python -m uv run python check_subscription.py
"""
import os
import requests
from dotenv import load_dotenv

load_dotenv()

token = os.environ.get("META_ACCESS_TOKEN")
# Real production WhatsApp Business Account ID (was the test WABA
# 2903100353357938 before we switched to a real registered number).
waba_id = "2160066441578508"
api_version = os.environ.get("GRAPH_API_VERSION", "v25.0")

url = f"https://graph.facebook.com/{api_version}/{waba_id}/subscribed_apps"

print("--- Checking current subscription ---")
resp = requests.get(url, headers={"Authorization": f"Bearer {token}"})
print(f"Status: {resp.status_code}")
print(f"Body: {resp.text}")

print("\n--- (Re-)subscribing this app ---")
resp2 = requests.post(url, headers={"Authorization": f"Bearer {token}"})
print(f"Status: {resp2.status_code}")
print(f"Body: {resp2.text}")
