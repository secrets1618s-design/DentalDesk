"""
Local terminal chat harness for testing the DentalDesk agent without WhatsApp.

This lets you have a real conversation with the assistant directly in your
terminal, so you can check that everything (Claude, the database, the
booking tools) works before ever connecting a real WhatsApp number.

Run it with:
    uv run python -m app.chat_test
(or, on Windows if `uv` isn't on your PATH: python -m uv run python -m app.chat_test)

Type 'quit' to exit.
"""
import asyncio
import logging

from dotenv import load_dotenv
load_dotenv()

from shared.logger_config import setup_logging
setup_logging()

from shared import db, clinics_store
from shared.models import Patient
from shared.message_utils import extract_reply_text
from app.agent import ClinicWorker

from mcp import ClientSession
from mcp.client.stdio import stdio_client
from langchain_core.messages import HumanMessage
from langgraph.graph import START

logger = logging.getLogger(__name__)

TEST_PHONE_NUMBER = "terminal-test-user"


async def main():
    # This app can run several clinics at once now (see app/agent.py's
    # ClinicWorker) -- for this terminal test harness, just use whichever
    # clinic is already configured (the legacy single-clinic .env vars, or
    # the first one added via /admin/clinics).
    clinics_store.init_control_db()
    clinics_store.migrate_legacy_single_clinic_if_needed()
    clinics = clinics_store.list_clinics()
    if not clinics:
        print(
            "No clinic is configured yet. Either set META_ACCESS_TOKEN and "
            "META_PHONE_NUMBER_ID in your .env (for the original single-clinic "
            "setup), or add a clinic via /admin/clinics first."
        )
        return
    clinic = clinics[0]
    print(f"Using clinic: {clinic['name']} ({clinic['slug']})")

    worker = ClinicWorker(clinic)
    db.set_current_db_path(clinic["db_path"])

    # Make sure the database exists and has the sample dentists in it.
    db.init_db(seed=True, dentists=clinic.get("dentists"))

    # Find or create a fake "patient" for this terminal session, the same
    # way a real WhatsApp message would.
    patient = db.get_patient_by_phone(TEST_PHONE_NUMBER)
    if not patient:
        patient = db.create_patient(Patient(name="New Patient", phone_number=TEST_PHONE_NUMBER))

    conversation = db.get_open_conversation(patient.id)
    if not conversation:
        conversation = db.create_conversation(patient.id)

    print("Starting the MCP server and connecting the agent...")

    async with stdio_client(worker.server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            agent = await worker.create_graph(session)

            config = {"configurable": {"thread_id": str(conversation.id)}}

            # Seed the conversation state with who we're talking to, exactly
            # like a real incoming WhatsApp message would.
            await agent.aupdate_state(
                config,
                {"patient": patient, "conversation_id": conversation.id},
                START,
            )

            print("\nConnected! Type a message below and press Enter.")
            print("Type 'quit' to exit.\n")

            while True:
                user_input = input("You: ").strip()
                if user_input.lower() in ("quit", "exit"):
                    break
                if not user_input:
                    continue

                response = await agent.ainvoke(
                    {"messages": [HumanMessage(content=user_input)]},
                    config,
                )
                # Claude sometimes attaches an internal "thinking" block
                # alongside its answer, which turns .content into a list of
                # content blocks instead of a plain string — extract just the
                # actual reply text so the terminal shows a clean message
                # instead of raw Python data.
                reply = extract_reply_text(response["messages"][-1].content)
                print(f"\nSia: {reply}\n")


if __name__ == "__main__":
    asyncio.run(main())
