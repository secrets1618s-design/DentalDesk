"""
One-off diagnostic script: sends a plain WhatsApp text message using the
same credentials and endpoint as src/app/whatsapp.py, so we can see the
full error response from Meta without fighting PowerShell's quoting rules.

Usage:
    python -m uv run python test_send.py
"""
import os
import requests
from dotenv import load_dotenv

load_dotenv()

token = os.environ.get("META_ACCESS_TOKEN")
phone_id = os.environ.get("META_PHONE_NUMBER_ID")
api_version = os.environ.get("GRAPH_API_VERSION")

print(f"Using phone_id={phone_id}, api_version={api_version}")
print(f"Token starts with: {token[:12]}..." if token else "Token is EMPTY/missing!")

print("\n--- Checking token permissions/expiry via debug_token ---")
debug_url = "https://graph.facebook.com/debug_token"
debug_resp = requests.get(debug_url, params={"input_token": token, "access_token": token})
print(f"debug_token status: {debug_resp.status_code}")
print(f"debug_token body:\n{debug_resp.text}")

url = f"https://graph.facebook.com/{api_version}/{phone_id}/messages"
headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json",
}

print("\n--- Attempt 1: TEMPLATE message (same as the working dashboard test) ---")
template_data = {
    "messaging_product": "whatsapp",
    "to": "966592965759",
    "type": "template",
    "template": {
        "name": "jaspers_market_order_confirmation_v1",
        "language": {"code": "en_US"},
        "components": [{
            "type": "body",
            "parameters": [
                {"type": "text", "text": "John Doe"},
                {"type": "text", "text": "123456"},
                {"type": "text", "text": "Sep 15, 2026"},
            ],
        }],
    },
}
resp1 = requests.post(url, json=template_data, headers=headers)
print(f"Status code: {resp1.status_code}")
print(f"Response body:\n{resp1.text}")

print("\n--- Attempt 2: plain TEXT message ---")
text_data = {
    "messaging_product": "whatsapp",
    "to": "966592965759",
    "type": "text",
    "text": {"body": "test from Mawaid diagnostic script"},
}
resp2 = requests.post(url, json=text_data, headers=headers)
print(f"Status code: {resp2.status_code}")
print(f"Response body:\n{resp2.text}")
