import logging
import json
import hmac
import hashlib
import os, getpass
import sys
import asyncio
from dotenv import load_dotenv
from shared.logger_config import setup_logging
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.staticfiles import StaticFiles
from . import whatsapp as whatsapp
from . import agent as agent_process

load_dotenv()
setup_logging()

def _set_env(var: str):
    if os.environ.get(var):
        return
    if sys.stdin is not None and sys.stdin.isatty():
        # Running interactively (e.g. a developer's own terminal) with the
        # variable missing from .env — prompt for it, same as before.
        os.environ[var] = getpass.getpass(f"{var}: ")
    else:
        # Running non-interactively (a cloud host, a background process) —
        # there's no one to answer a prompt, so getpass would just hang
        # forever with no explanation. Fail fast with a clear error instead.
        raise RuntimeError(
            f"Required environment variable '{var}' is not set. Set it in your "
            "hosting platform's environment variables (or in .env for local runs)."
        )

# incase env vars are not set, prompt for them (only when running interactively)
_set_env("ANTHROPIC_API_KEY")
_set_env("META_ACCESS_TOKEN")
_set_env("META_APP_SECRET")
_set_env("GRAPH_API_VERSION")
_set_env("META_PHONE_NUMBER_ID")
_set_env("META_VERIFY_TOKEN")

# Get a logger for this module
logger = logging.getLogger(__name__)

app = FastAPI()

# Serves files from the static/ folder (e.g. the offers/promotions brochure
# image) at public URLs like <your-app-url>/static/brochure.jpg — this is
# what lets Sia send that image over WhatsApp, since WhatsApp needs a real,
# publicly reachable URL rather than a local file path. To change the
# brochure, just replace static/brochure.jpg (see config/clinic_config.yaml).
STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
async def startup_event():
    logger.info("Starting agent consumer process in the background...")
    asyncio.create_task(agent_process.main())


def verify_signature(request: Request):
    logger.debug("Verifying request signature")

    signature = request.headers.get("X-Hub-Signature-256", "")[7:]

    async def get_body():
        return await request.body()

    body = asyncio.run(get_body())

    digest = hmac.new(
        bytes(os.environ.get("META_APP_SECRET"), "latin-1"),
        msg=body,
        digestmod=hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(digest, signature):
        logger.error("Signature verification failed!")
        raise HTTPException(status_code=403, detail="signature is not valid")
    return True


@app.get("/webhook")
async def verify_webhook(request: Request):
    logger.info("Received a GET request on /webhook for verification")

    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode and token and challenge:
        if mode == "subscribe" and token == os.environ.get("META_VERIFY_TOKEN"):
            logger.info("whatsapp webhook verified successfully")
            return int(challenge)
        else:
            logger.error("whatsapp webhook verification failed - invalid token")
            raise HTTPException(status_code=403, detail="Forbidden")
    else:
        logger.error("whatsapp webhook verification failed - missing parameters")
        raise HTTPException(status_code=400, detail="Missing parameters for verification")


@app.post("/webhook")
async def receive_webhook(request: Request, signature_valid: bool = Depends(verify_signature)):
    logger.debug("Received a POST request on /webhook")

    try:
        body = await request.json()
    except json.JSONDecodeError:
        logger.error("Failed to decode JSON from request body")
        raise HTTPException(status_code=400, detail="Invalid JSON provided in request body")

    logger.debug(f"request body: {body}")

    # Respond to status updates (like message delivered, read etc.)
    if whatsapp.is_status_update(body):
        status = whatsapp.parse_status_update(body)
        # Logged at INFO (not debug) and with the full status object --
        # temporary/diagnostic-friendly change so delivery failures (which
        # include an "errors" field with the real reason) show up in
        # Railway's log viewer without digging through debug-level noise.
        logger.info(f"WhatsApp status update: {status}")
        return {"status": "ok"}

    try:
        if whatsapp.is_valid_message(body) and whatsapp.is_text_message(body):
            phone_number, message_body = whatsapp.parse_phone_and_message(body)
            logger.info(f"Incoming message from {phone_number}: {message_body}")
            await agent_process.enqueue_message(phone_number, message_body)

            return {"status": "ok"}
        elif whatsapp.is_valid_message(body):
            # A real message, but not plain text (voice note, image, sticker,
            # reaction, location, etc) -- Sia/the agent pipeline only handles
            # text today. Previously this fell through to
            # parse_phone_and_message(), which raised and turned into a 400
            # response to Meta -- repeated failed webhook deliveries risk
            # Meta throttling/disabling the subscription entirely (same
            # class of issue as the "unhandled event" case below). Instead:
            # acknowledge safely and let the patient know in their own chat
            # why nothing happened, rather than silently ignoring them.
            msg_type = body["entry"][0]["changes"][0]["value"]["messages"][0].get("type")
            sender = whatsapp.get_message_sender(body)
            logger.info(f"Received unsupported message type '{msg_type}' from {sender} -- only text messages are handled today.")
            if sender:
                try:
                    whatsapp.send_message(
                        sender,
                        "Sorry, I can only read text messages right now — could you type your message instead? 🙏\n"
                        "عذرًا، يمكنني حاليًا قراءة الرسائل النصية فقط، هل يمكنك كتابة رسالتك؟",
                    )
                except Exception as e:
                    logger.error(f"Failed to send unsupported-message-type notice to {sender}: {e}")
            return {"status": "ok"}
        else:
            # Not a text message we recognize (could be a reaction, a
            # template-quality/webhook-test ping, a read receipt shape we
            # don't otherwise catch, etc). Log the full body at INFO so we
            # can see exactly what it was, but respond 200 OK -- returning
            # an error here for event types we simply don't act on yet is
            # what was causing Meta to see repeated failed webhook
            # deliveries, which risks Meta throttling/disabling the
            # webhook subscription entirely. Always acknowledge safely.
            logger.info(f"Unhandled webhook event (not a text message or status update): {body}")
            return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error processing message: {e}")
        raise HTTPException(status_code=400, detail="Error processing message")


def main():
    import uvicorn
    # Most cloud hosts (Railway, Render, Heroku, etc.) assign a port at
    # runtime via the PORT environment variable and require the app to
    # listen on it. Prefer that when present; otherwise fall back to
    # FAST_API_PORT (or 8000) for local development.
    port = int(os.environ.get("PORT") or os.environ.get("FAST_API_PORT", 8000))
    logger.info("Starting FastAPI server on port %s...", port)
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
