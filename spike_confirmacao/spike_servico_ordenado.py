"""SessionService experimental que preserva a ordem de append no SQLite.

APIs públicas usadas do ADK 2.9.2:

* ``DatabaseSessionService.get_session(..., config=None)`` para reler todos os
  eventos já gravados antes do primeiro append feito por este objeto;
* ``DatabaseSessionService.append_event`` como ponto público sobrescrevível;
* ``Session.events`` e ``Event.timestamp`` como campos públicos.

Nenhuma API interna/privada do ADK é usada. A solução, porém, depende do
comportamento interno observado de releitura por ``(timestamp, id)``.

Premissas do spike: há um único processo escritor para uma sessão em cada
instante; o relógio de parede pode regredir, pois o timestamp é elevado em dois
microssegundos (acima da resolução persistida pelo SQLite), mas processos
escritores concorrentes não são coordenados. O dicionário de ``asyncio.Lock``
protege, por sessão e neste processo, a leitura inicial, o ajuste e o append. O
SQLite continua sendo apenas o backend do experimento, não uma recomendação de
produção.
"""

from __future__ import annotations

import asyncio

from google.adk.events import Event
from google.adk.sessions import DatabaseSessionService
from google.adk.sessions.session import Session


class OrderedDatabaseSessionService(DatabaseSessionService):
    """Impõe timestamps estritamente crescentes na ordem de append da sessão."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._last_timestamp: dict[tuple[str, str, str], float] = {}
        self.timestamp_adjustments = 0
        self.persisted_timestamp_reads = 0

    async def append_event(self, session: Session, event: Event) -> Event:
        key = (session.app_name, session.user_id, session.id)
        lock = self._session_locks.setdefault(session.id, asyncio.Lock())

        async with lock:
            if key not in self._last_timestamp:
                persisted = await super().get_session(
                    app_name=session.app_name,
                    user_id=session.user_id,
                    session_id=session.id,
                    config=None,
                )
                persisted_events = persisted.events if persisted is not None else []
                self._last_timestamp[key] = max(
                    (stored.timestamp for stored in persisted_events),
                    default=float("-inf"),
                )
                self.persisted_timestamp_reads += 1

            previous = self._last_timestamp[key]
            if event.timestamp <= previous:
                # O StorageEvent do SQLite ordena um datetime com resolução de
                # microssegundos. ``math.nextafter`` é estrito em memória, mas
                # arredonda para empate ao persistir; 2 µs preserva a ordem.
                event.timestamp = previous + 0.000002
                self.timestamp_adjustments += 1

            appended = await super().append_event(session=session, event=event)
            self._last_timestamp[key] = event.timestamp
            return appended
