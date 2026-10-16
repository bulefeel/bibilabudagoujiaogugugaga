"""Classify an already received response; never issue or replay a request."""

import asyncio
import re
from typing import Any


async def is_csrf_header_loss(response: Any, *, timeout: float) -> bool:
    """Return a boolean only; credentials and response contents stay transient.

    Callers restrict this to the exact inventory endpoint's HTTP 403 responses.
    A status alone, or a missing header alone, is insufficient for recovery.
    """
    async with asyncio.timeout(timeout):
        if response.request.method != "POST":
            return False
        names = {name.lower() for name in response.request.headers}
        if "anti-csrftoken-a2z" in names or "content-type" in names:
            return False
        if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "text/html":
            return False
        body = await response.text()
        # Captured error pages have an Amazon document title and the decisive
        # message in a heading; match either, never arbitrary script/body text.
        headings = re.findall(r"<(title|h[1-6])\b[^>]*>\s*([^<]*)\s*</\1\s*>", body, re.IGNORECASE)
        return any(text.strip() == "Unsafe cross site request." for _, text in headings)
