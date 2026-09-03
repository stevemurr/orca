"""Textual command-palette provider backed by the slash command catalogue."""

from __future__ import annotations

from functools import partial
from typing import override

from textual.command import DiscoveryHit, Hit, Hits, Provider

from orca.app.commands import visible_commands
from orca.tui.host import ModelHost, model_host


class OrcaCommands(Provider):
    @property
    def _host(self) -> ModelHost:
        return model_host(self.app)

    @override
    async def discover(self) -> Hits:
        host = self._host
        for command in visible_commands(developer=host.model.developer):
            yield DiscoveryHit(
                f"/{command.name}",
                partial(host.invoke_command, command.name, ""),
                help=command.summary,
            )

    @override
    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        host = self._host
        for command in visible_commands(developer=host.model.developer):
            candidate = f"/{command.name}"
            score = matcher.match(candidate + " " + command.summary)
            if score <= 0:
                continue
            yield Hit(
                score,
                matcher.highlight(candidate),
                partial(host.invoke_command, command.name, ""),
                text=candidate,
                help=command.summary,
            )
