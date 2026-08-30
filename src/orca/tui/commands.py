"""Textual command-palette provider backed by the slash command catalogue."""

from __future__ import annotations

from functools import partial

from textual.command import DiscoveryHit, Hit, Hits, Provider

from orca.app.commands import visible_commands


class OrcaCommands(Provider):
    async def discover(self) -> Hits:
        developer = bool(getattr(self.app, "model", None).developer)
        for command in visible_commands(developer=developer):
            yield DiscoveryHit(
                f"/{command.name}",
                partial(self.app.invoke_command, command.name, ""),  # type: ignore[attr-defined]
                help=command.summary,
            )

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        developer = bool(getattr(self.app, "model", None).developer)
        for command in visible_commands(developer=developer):
            candidate = f"/{command.name}"
            score = matcher.match(candidate + " " + command.summary)
            if score <= 0:
                continue
            yield Hit(
                score,
                matcher.highlight(candidate),
                partial(self.app.invoke_command, command.name, ""),  # type: ignore[attr-defined]
                text=candidate,
                help=command.summary,
            )
