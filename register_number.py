"""
Completes WhatsApp Cloud API registration for the production phone number.

Adding + verifying a number in the Meta dashboard is NOT always enough on
its own -- Meta also requires an explicit call to the /register endpoint to
fully activate the number on the WhatsApp network. Without this step, the
number can show as "Registered" in the dashboard's own checklist while
still being unreachable to real WhatsApp users (looking unregistered /
"Invite to WhatsApp" when someone tries to message it).

Usage:
    uv run python register_number.py
"""
import os
import requests
from dotenv import load_dotenv

load_dotenv()

token = os.environ.get("META_ACCESS_TOKEN")
phone_number_id = os.environ.get("META_PHONE_NUMBER_ID")
api_version = os.environ.get("GRAPH_API_VERSION", "v25.0")

# A 6-digit PIN for this number's two-step verification. This can be any 6
# digits you choose -- Meta may ask you to re-enter this PIN in the future
# if you ever need to re-register this number, so make a note of it.
PIN = "301280"

print("--- Checking current phone number status ---")
status_url = (
    f"https://graph.facebook.com/{api_version}/{phone_number_id}"
    "?fields=verified_name,code_verification_status,quality_rating,platform_type,status,"
    "name_status,is_pin_enabled,account_mode"
)
status_resp = requests.get(status_url, headers={"Authorization": f"Bearer {token}"})
print(f"Status: {status_resp.status_code}")
print(f"Body: {status_resp.text}")

print("\n--- Registering phone number for Cloud API messaging ---")
register_url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/register"
register_resp = requests.post(
    register_url,
    headers={"Authorization": f"Bearer {token}"},
    json={"messaging_product": "whatsapp", "pin": PIN},
)
print(f"Status: {register_resp.status_code}")
print(f"Body: {register_resp.text}")
