"""
Small helper for reading the text out of an LLM reply.

Claude's raw message content is usually a plain string, but when the model
includes an internal reasoning ("thinking") step alongside its answer, the
content instead comes back as a list of content blocks — a mix of
{"type": "thinking", ...} and {"type": "text", "text": "..."} dicts. If that
list is used as-is (saved to the database, or sent as a WhatsApp reply), the
patient/user sees raw Python data instead of Sia's actual answer — and since
the database column this gets saved into is plain text, saving a list there
directly can fail outright.

extract_reply_text() always returns a clean string, safe to save and safe to
send, regardless of which shape Claude returned.
"""
from typing import Any


def extract_reply_text(content: Any) -> str:
    """Pulls the user-facing text out of an LLM message's .content.

    Handles the plain-string case (most of the time) and the list-of-blocks
    case (when Claude includes a 'thinking' block), returning only the
    'text' block(s) joined together. Any other content shape is coerced to
    a string as a last resort rather than silently dropped.
    """
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                # Other block types (e.g. "thinking") are intentionally
                # skipped — they're not meant for the end user.
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts).strip()

    if content:
        return str(content)

    return ""
