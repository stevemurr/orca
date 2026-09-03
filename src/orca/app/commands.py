"""One command catalogue shared by completion, help, and dispatch."""

from __future__ import annotations

from dataclasses import dataclass


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
