"""Conservative terminal keyboard capability detection."""
from __future__ import annotations

import os
from collections.abc import Mapping


def supports_modified_enter(env: Mapping[str, str] | None = None) -> bool:
    """Return True only when modified Enter reporting is known to be reliable.

    Terminals don't expose a portable synchronous capability query at startup. Coomi
    therefore treats uncertain environments as unsupported and advertises Ctrl+J.
    """
    values = os.environ if env is None else env
    override = values.get("COOMI_MODIFIED_ENTER", "").strip().casefold()
    if override in {"1", "true", "yes", "on"}:
        return True
    if override in {"0", "false", "no", "off"}:
        return False

    term_program = values.get("TERM_PROGRAM", "").casefold()
    term = values.get("TERM", "").casefold()
    if values.get("KITTY_WINDOW_ID") or "kitty" in term_program or "kitty" in term:
        return True
    if values.get("GHOSTTY_RESOURCES_DIR") or "ghostty" in term_program:
        return True
    return False
