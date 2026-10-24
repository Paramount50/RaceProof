from __future__ import annotations

import os
import re


def redact_sensitive(value: object, *extra_secrets: str | None) -> str:
    text = str(value)
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY"):
        secret = os.environ.get(name)
        if secret and secret != "placeholder":
            text = text.replace(secret, "[REDACTED]")
    for secret in extra_secrets:
        if secret and secret != "placeholder":
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(postgresql(?:\+\w+)?://[^:/\s]+:)[^@/\s]+(@)", r"\1[REDACTED]\2", text)
    text = re.sub(r"(?i)(api[_ -]?key[=: ]+)[^\s,;]+", r"\1[REDACTED]", text)
    return text
