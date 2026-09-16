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
        logger.debug(f"Received a WhatsApp status update event. status = {status.get('status')}")
        return {"status": "ok"}

    try:
        if whatsapp.is_valid_message(body):
            phone_number, message_body = whatsapp.parse_phone_and_message(body)
            logger.info(f"Incoming message from {phone_number}: {message_body}")
            await agent_process.enqueue_message(phone_number, message_body)

            return {"status": "ok"}
        else:
            logger.error("Invalid WhatsApp message structure")
            raise HTTPException(status_code=404, detail="Invalid WhatsApp message structure")
        
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
