"""One command catalogue shared by completion, help, and dispatch."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from orca.app.model import Choice


@dataclass(frozen=True, slots=True)
class CommandSpec:
    name: str
    summary: str
    argument: str = ""
    developer_only: bool = False


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    name: str
    argument: str


@dataclass(frozen=True, slots=True)
class Suggestion:
    """One row of the menu that opens on `/`.

    A command, or -- once a command that takes a value is named and the backend has said
    which values it accepts -- one of those values. `insert` is what Tab puts in the
    composer; `runnable` says whether Enter can run the row as it stands, or has to hand it
    to the person to finish.
    """

    name: str
    label: str
    argument: str
    summary: str
    insert: str
    runnable: bool


#: The values each command accepts, by command name, as the backend advertised them. A
#: command not listed, or listed with nothing, takes whatever is typed.
Choices = Mapping[str, Sequence[Choice]]

COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec("chat", "return to the conversation"),
    CommandSpec("review", "inspect the result and artifacts"),
    CommandSpec("threads", "browse recent conversations"),
    CommandSpec("new", "start a new conversation"),
    CommandSpec("resume", "continue the active paused run"),
    CommandSpec("pause", "pause at the next durable checkpoint"),
    CommandSpec("cancel", "cancel the active attempt"),
    CommandSpec("mode", "set the mode for future turns", "<mode>"),
    CommandSpec("permissions", "set the approval policy", "<policy>"),
    CommandSpec("workspace", "show or switch the active workspace", "<path>"),
    CommandSpec("add", "reach one more folder from this conversation", "<path>"),
    CommandSpec("status", "show connection and run status"),
    CommandSpec("tools", "show every tool call, or fold them again"),
    CommandSpec("agents", "show the delegated agents and their work"),
    CommandSpec("help", "show commands and keyboard shortcuts"),
    CommandSpec("inspect", "open developer events and traces", developer_only=True),
    CommandSpec("quit", "leave; durable work keeps running"),
)

_COMMANDS_BY_NAME = {command.name: command for command in COMMANDS}


def parse_input(value: str) -> ParsedCommand | None:
    """Parse only known commands; absolute paths remain ordinary user input."""

    if not value.startswith("/") or len(value) < 2:
        return None
    head, _, argument = value[1:].partition(" ")
    if head not in _COMMANDS_BY_NAME:
        return None
    return ParsedCommand(head, argument.strip())


def visible_commands(*, developer: bool = False) -> tuple[CommandSpec, ...]:
    return tuple(command for command in COMMANDS if developer or not command.developer_only)


def spec_for(name: str) -> CommandSpec | None:
    return _COMMANDS_BY_NAME.get(name)


def argument_label(command: CommandSpec, values: Sequence[Choice]) -> str:
    """`<policy>`, or the values that stand in for it once the backend has named them."""
    return " | ".join(value.name for value in values) if values else command.argument


def suggest(
    draft: str,
    *,
    developer: bool = False,
    choices: Choices | None = None,
    skills: Sequence[Choice] = (),
) -> tuple[Suggestion, ...]:
    """The rows a draft could become, for a menu that opens on `/`.

    While the draft is a slash and the start of a name, the commands that start that way,
    then the workspace's skills that do: a skill is invoked as `/name`, like a command, and
    the backend reads its instructions as the request. Once a name and a space follow, the
    values the backend accepts for that command, if it said -- so `/permissions ` shows
    what a policy can be rather than leaving a person to guess. Nothing once a message or
    a second word begins: a menu over either is in the way.
    """
    if not draft.startswith("/") or "\n" in draft:
        return ()
    offered = choices or {}
    head, named, rest = draft[1:].partition(" ")
    head = head.lower()
    if not named:
        commands = tuple(
            _command_row(command, offered.get(command.name, ()))
            for command in visible_commands(developer=developer)
            if command.name.startswith(head)
        )
        return commands + tuple(
            _skill_row(skill)
            for skill in skills
            if skill.name.startswith(head) and skill.name not in _COMMANDS_BY_NAME
        )
    command = _COMMANDS_BY_NAME.get(head)
    values = offered.get(head, ())
    if command is None or not values or " " in rest:
        return ()
    return tuple(
        Suggestion(
            name=value.name,
            label=value.name,
            argument="",
            # What the value means, when the backend said; the command it runs otherwise.
            summary=value.summary or f"/{command.name} {value.name}",
            insert=f"/{command.name} {value.name}",
            runnable=True,
        )
        for value in values
        if value.name.lower().startswith(rest.lower())
    )


def _skill_row(skill: Choice) -> Suggestion:
    """A skill as a menu row. Enter puts `/name ` in the composer rather than sending it,
    so what to apply the skill to can follow; a second Enter sends it as it stands."""
    return Suggestion(
        name=skill.name,
        label=f"/{skill.name}",
        argument="[request]",
        summary=skill.summary or "a skill of this workspace",
        insert=f"/{skill.name} ",
        runnable=False,
    )


def _command_row(command: CommandSpec, values: Sequence[Choice]) -> Suggestion:
    return Suggestion(
        name=command.name,
        label=f"/{command.name}",
        argument=argument_label(command, values),
        summary=command.summary,
        insert=f"/{command.name}" + (" " if command.argument else ""),
        runnable=not command.argument,
    )
