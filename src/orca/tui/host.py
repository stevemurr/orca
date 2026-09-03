"""What a screen or command provider may ask of the application that owns it.

Textual hands a screen or a command provider its application as a bare `App`, which knows
nothing about orca's model. The two things they need -- the current state, and a way to
dispatch an action into the reducer -- are small enough to name here, so neither module has
to import `orca.tui.app` and complete a cycle with it.
"""

from __future__ import annotations

from typing import Protocol, cast

from orca.app.actions import Action
from orca.app.model import AppState


class ModelHost(Protocol):
    """The application as its screens and palette see it: one store and one dispatcher."""

    model: AppState

    def apply_model_action(self, action: Action) -> None: ...

    def invoke_command(self, name: str, argument: str = "") -> None: ...


def model_host(app: object) -> ModelHost:
    """The owning application, seen through the host interface.

    Textual types `self.app` as a bare `App`, and the only application that ever mounts orca's
    screens or registers its palette is `OrcaApp`, which satisfies this protocol. This is the
    one place that claim is made without being checked.
    """
    return cast(ModelHost, app)
