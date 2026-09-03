"""The shape of anything decoded from JSON, named so nothing has to be `Any`.

Every value that crosses the wire -- an event payload, a workspace record, a capabilities
document, a TOML profile table -- is one of these. Reading one is a matter of `.get` and an
`isinstance` check, which is exactly what the code did already; the alias only lets the
checker follow along instead of giving up at the first `Any`.

The container members are the abstract `Mapping` and `Sequence` rather than `dict` and `list`
on purpose. Both are covariant, so a `dict[str, str]` literal in a test is a `JsonObject`
without a cast, where `dict[str, JsonValue]` would refuse it because `dict` is invariant.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

type JsonValue = Mapping[str, JsonValue] | Sequence[JsonValue] | str | int | float | bool | None
type JsonObject = Mapping[str, JsonValue]
