import logging
from fastapi import HTTPException
import json
import re
import requests
import os

logger = logging.getLogger(__name__)


def is_status_update(body):
    """
    Check if the incoming webhook event is a WhatsApp status update.
    """
    return (
        body.get("object")
        and body.get("entry")
        and body["entry"][0].get("changes")
        and body["entry"][0]["changes"][0].get("value")
        and body["entry"][0]["changes"][0]["value"].get("statuses")
    )


def parse_status_update(body):
    """
    Parse the WhatsApp status update from the webhook event.
    """
    try:
        status = body["entry"][0]["changes"][0]["value"]["statuses"][0]
        logger.debug(f"WhatsApp Status - {status}")
        return status
    
    except Exception as e:
        logger.error(f"Error parsing status update: {e}")
        raise HTTPException(status_code=400, detail="Invalid WhatsApp status update structure")
    

def is_valid_message(body):
    """
    Check if the incoming webhook event is a message WhatsApp event at all
    (of any type -- text, image, voice note, sticker, reaction, location,
    etc). Use `is_text_message` to further check it's specifically a text
    message before calling `parse_phone_and_message` on it.
    """
    return (
        body.get("object")
        and body.get("entry")
        and body["entry"][0].get("changes")
        and body["entry"][0]["changes"][0].get("value")
        and body["entry"][0]["changes"][0]["value"].get("messages")
        and body["entry"][0]["changes"][0]["value"]["messages"][0]
    )


def is_text_message(body):
    """
    Check if the incoming message webhook event is specifically a plain
    text message. Assumes `is_valid_message(body)` is already True.
    """
    try:
        return body["entry"][0]["changes"][0]["value"]["messages"][0].get("type") == "text"
    except Exception:
        return False


def get_message_sender(body):
    """
    Extracts the sender's phone number from a message webhook event,
    regardless of the message type -- used so we can still reply to a
    patient who sent something other than plain text (e.g. a voice note or
    image), which `parse_phone_and_message` deliberately doesn't support.
    Returns None if it can't be found.
    """
    try:
        return body["entry"][0]["changes"][0]["value"]["messages"][0].get("from")
    except Exception:
        return None


def get_receiving_phone_number_id(body):
    """
    Extracts the WhatsApp Business phone_number_id this event was sent TO
    (not the patient's own number) from a webhook event's metadata --
    present on both message and status-update events. This is what lets
    ONE shared /webhook endpoint tell multiple clinics' numbers apart: each
    clinic's WhatsApp number has a different phone_number_id, and every
    incoming event says which one it arrived on.
    Returns None if it can't be found.
    """
    try:
        return body["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"]
    except Exception:
        return None


def parse_phone_and_message(body):
    """
    Parse the phone number and message body from the WhatsApp webhook event.
    Only call this after confirming `is_text_message(body)` is True.
    """
    try:
        obj = body["entry"][0]["changes"][0]["value"]["messages"][0]

        phone_number = obj["from"]  # extract the phone number of the sender
        message_body = obj["text"]["body"]  # extract the text message body
        return phone_number, message_body

    except Exception as e:
        logger.error(f"Error parsing phone number and message: {e}")
        raise HTTPException(status_code=400, detail="Invalid WhatsApp message structure")


def send_message(phone_number, message, access_token=None, phone_number_id=None, api_version=None):
    """
    Sends a plain text WhatsApp message.

    access_token / phone_number_id / api_version let a caller send on
    behalf of a SPECIFIC clinic (needed now that one running app can serve
    several clinics, each with their own WhatsApp number/token) -- when
    omitted, this falls back to the single-clinic environment variables,
    exactly as before, which is what the per-clinic MCP tool subprocess
    still relies on (it always has exactly one clinic's credentials in its
    own environment already).
    """
    message = format_message_content(message)
    data = json.dumps({
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": phone_number,
            "type": "text",
            "text": {"preview_url": False, "body": message},
        })

    headers = {
        "Content-type": "application/json",
        "Authorization": f"Bearer {access_token or os.environ.get('META_ACCESS_TOKEN')}",
    }

    api_version = api_version or os.environ.get("GRAPH_API_VERSION")
    phone_id = phone_number_id or os.environ.get("META_PHONE_NUMBER_ID")
    url = f"https://graph.facebook.com/{api_version}/{phone_id}/messages"

    try:
        response = requests.post(url, data=data, headers=headers)
        response.raise_for_status()
    except requests.Timeout:
        logger.error("Whatsapp send message request timed out")
        raise HTTPException(status_code=408, detail="Request Timeout")
    except requests.RequestException as e:
        # raise_for_status()'s own exception text doesn't include the
        # response body, but Meta's Graph API puts the actual reason
        # (invalid/expired token, missing permission, wrong asset, etc)
        # in a JSON "error" object in the body -- log it explicitly so
        # a plain "401 Unauthorized" in the logs doesn't hide why.
        body = response.text if 'response' in locals() else "<no response>"
        logger.error(f"Internal Server Error, failed to send message : {e} | response body: {body}")
        raise HTTPException(status_code=500, detail="Internal Server Error, failed to send message")
    else:
        logger.debug(f"send_message - status: {response.status_code}")
        logger.info(f"Outgoing message to {phone_number}: {message}")

        return response


def send_image_message(phone_number, image_url, caption=None, access_token=None, phone_number_id=None, api_version=None):
    """
    Sends an image message (e.g. the offers/promotions brochure) to a
    patient over WhatsApp. `image_url` must be a real, publicly reachable
    URL — WhatsApp fetches the image from it directly, it cannot be a
    local file path.

    See send_message() above for why access_token / phone_number_id /
    api_version exist and when to pass them.
    """
    image_payload = {"link": image_url}
    if caption:
        image_payload["caption"] = caption

    data = json.dumps({
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": phone_number,
            "type": "image",
            "image": image_payload,
        })

    headers = {
        "Content-type": "application/json",
        "Authorization": f"Bearer {access_token or os.environ.get('META_ACCESS_TOKEN')}",
    }

    api_version = api_version or os.environ.get("GRAPH_API_VERSION")
    phone_id = phone_number_id or os.environ.get("META_PHONE_NUMBER_ID")
    url = f"https://graph.facebook.com/{api_version}/{phone_id}/messages"

    try:
        response = requests.post(url, data=data, headers=headers)
        response.raise_for_status()
    except requests.Timeout:
        logger.error("Whatsapp send image message request timed out")
        raise HTTPException(status_code=408, detail="Request Timeout")
    except requests.RequestException as e:
        body = response.text if 'response' in locals() else "<no response>"
        logger.error(f"Internal Server Error, failed to send image message : {e} | response body: {body}")
        raise HTTPException(status_code=500, detail="Internal Server Error, failed to send image message")
    else:
        logger.debug(f"send_image_message - status: {response.status_code}")
        logger.info(f"Outgoing image to {phone_number}: {image_url}")
        return response


def format_message_content(text: str) -> str:
    """
    Cleans and converts input text into WhatsApp-compatible formatting.
    - Removes 【...】 blocks
    - Converts Markdown styles to WhatsApp equivalents
    """

    # 1. Remove brackets and their content 【...】
    text = re.sub(r"\【.*?\】", "", text).strip()

    # 2. Convert Markdown bold (**text**) → WhatsApp bold (*text*)
    text = re.sub(r"\*\*(.*?)\*\*", r"*\1*", text)

    # 3. Convert Markdown italics (_text_) → WhatsApp italics (_text_)
    # (ensure it doesn't conflict with bold/underscore usage)
    text = re.sub(r"_(.*?)_", r"_\1_", text)

    # 4. Convert Markdown strikethrough (~~text~~) → WhatsApp (~text~)
    text = re.sub(r"~~(.*?)~~", r"~\1~", text)

    # 5. Convert Markdown inline code (`text`) → WhatsApp monospace (`text`)
    text = re.sub(r"`(.*?)`", r"`\1`", text)

    return text
