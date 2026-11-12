"""A stand-in for pika's BlockingChannel that records what a publisher does with it.

Pass it where your outbox relay expects a channel. It never talks to a broker. Declares are
checked as they happen: services must not create queues, and may declare only their own
exchange, exactly as docs/MESSAGING.md shows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ticketto_contracts import catalogue
from ticketto_contracts.validate import Problem, PublishedMessage, properties_dict


def _value(x: Any) -> Any:
    return getattr(x, "value", x)


@dataclass
class RecordingChannel:
    """Records basic_publish calls as PublishedMessage, and declare mistakes as violations."""

    service: str | None = None  # needed only to check exchange_declare
    published: list[PublishedMessage] = field(default_factory=list)
    violations: list[Problem] = field(default_factory=list)
    confirming: bool = False
    is_open: bool = True

    def confirm_delivery(self) -> None:
        self.confirming = True

    def basic_publish(
        self,
        exchange: str,
        routing_key: str,
        body: bytes | str,
        properties: Any = None,
        mandatory: bool = False,
    ) -> None:
        self.published.append(
            PublishedMessage(
                exchange=exchange,
                routing_key=routing_key,
                body=body.encode() if isinstance(body, str) else bytes(body),
                properties=properties_dict(properties),
                mandatory=mandatory,
                confirmed=self.confirming,
            )
        )

    def exchange_declare(  # pylint: disable=too-many-arguments,too-many-positional-arguments  # pika's
        self,
        exchange: str,
        exchange_type: Any = "direct",
        passive: bool = False,
        durable: bool = False,
        auto_delete: bool = False,
        internal: bool = False,
        arguments: dict[str, Any] | None = None,
    ) -> None:
        if passive:
            return
        own = catalogue.exchange_for(self.service) if self.service else None
        wrong = []
        if own is not None and exchange != own:
            wrong.append(f"not {self.service}'s exchange {own!r}")
        if _value(exchange_type) != "topic":
            wrong.append(f"type {_value(exchange_type)!r}, not 'topic'")
        if not durable or auto_delete or internal:
            wrong.append("must be durable=True, auto_delete=False, internal=False")
        if arguments:
            wrong.append(f"must have no arguments, got {arguments}")
        if wrong:
            self.violations.append(
                Problem("exchange_declare", f"exchange_declare({exchange!r}): {'; '.join(wrong)}")
            )

    def queue_declare(  # pylint: disable=too-many-arguments,too-many-positional-arguments  # pika's
        self,
        queue: str,
        passive: bool = False,
        durable: bool = False,
        exclusive: bool = False,
        auto_delete: bool = False,
        arguments: dict[str, Any] | None = None,
    ) -> None:
        del durable, exclusive, auto_delete, arguments
        if not passive:
            self.violations.append(
                Problem(
                    "queue_declare",
                    f"queue_declare({queue!r}) without passive=True: the broker owns its queues",
                )
            )

    def basic_qos(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs

    def close(self) -> None:
        self.is_open = False
