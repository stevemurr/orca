"""``orca`` — a terminal client for any harness that implements `orca.backend.TerminalBackend`.

Application state and views depend only on client-owned contract projections. Local process and
workspace discovery live behind the terminal backend port; execution belongs to the backend.
"""

from __future__ import annotations
