"""State-driven application core for the interactive orca client.

The package deliberately contains no terminal widgets and performs no I/O.  Ordered backend
events and user intents reduce into immutable state; effect objects describe the work an outer
runtime must perform.
"""

from orca.app.model import AppState

__all__ = ["AppState"]
