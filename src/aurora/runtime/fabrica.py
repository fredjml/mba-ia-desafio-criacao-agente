"""Fábrica de App resumable + Runner + OrderedDatabaseSessionService."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from google.adk.agents.base_agent import BaseAgent
from google.adk.apps import App, ResumabilityConfig
from google.adk.runners import Runner

from aurora.runtime.servico_ordenado import OrderedDatabaseSessionService


def _raiz_projeto() -> Path:
    for start in (Path(__file__).resolve().parent, Path.cwd()):
        for candidate in (start, *start.parents):
            if (candidate / "pyproject.toml").is_file():
                return candidate
    return Path(__file__).resolve().parents[3]


def caminho_banco_sessoes(explicito: str | os.PathLike[str] | None = None) -> Path:
    if explicito is not None:
        return Path(explicito)
    env = os.environ.get("AURORA_SESSION_DB")
    if env:
        return Path(env)
    return _raiz_projeto() / "var" / "sessions.sqlite3"


@dataclass
class RuntimeMontado:
    app: App
    runner: Runner
    session_service: OrderedDatabaseSessionService


def montar_runtime(
    root_agent: BaseAgent,
    *,
    session_db: str | os.PathLike[str] | None = None,
    app_name: str = "aurora",
) -> RuntimeMontado:
    """Monta App(resumable) + Runner + serviço ordenado.

    O caminho do banco vem do parâmetro, de AURORA_SESSION_DB ou, por
    padrão, ``var/sessions.sqlite3`` na raiz do projeto (E21 ignorará
    ``var/``). Não cria o diretório padrão durante testes: use TEMP.
    """
    db_path = caminho_banco_sessoes(session_db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    service = OrderedDatabaseSessionService(
        db_url=f"sqlite+aiosqlite:///{db_path.as_posix()}"
    )
    app = App(
        name=app_name,
        root_agent=root_agent,
        resumability_config=ResumabilityConfig(is_resumable=True),
    )
    return RuntimeMontado(
        app=app,
        runner=Runner(app=app, session_service=service),
        session_service=service,
    )
