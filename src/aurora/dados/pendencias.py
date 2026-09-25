"""Tabela própria de pendências de confirmação (D5).

Mora no MESMO SQLite do domínio (`caminho_banco()`). Schema sob demanda,
sem alterar dominio.py. busy_timeout e retry via `com_retry`.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aurora.dados.dominio import caminho_banco, com_retry, garantir_banco

TABELA = "pendencias"
STATUS_PENDENTE = "pendente"
STATUS_APROVADA = "aprovada"
STATUS_NEGADA = "negada"
STATUS_CANCELADA = "cancelada"
STATUS_VALIDOS = (
    STATUS_PENDENTE,
    STATUS_APROVADA,
    STATUS_NEGADA,
    STATUS_CANCELADA,
)


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat()


def _schema(connection: Any) -> None:
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {TABELA}("
        "session_id TEXT NOT NULL,"
        "id TEXT NOT NULL,"
        "acao TEXT NOT NULL,"
        "detalhes TEXT NOT NULL,"
        "status TEXT NOT NULL CHECK ("
        "status IN ('pendente','aprovada','negada','cancelada')),"
        "criado_em TEXT NOT NULL,"
        "respondido_em TEXT,"
        "PRIMARY KEY (session_id, id))"
    )


def garantir_tabela(db_path: str | Path | None = None) -> Path:
    path = garantir_banco(db_path)
    com_retry(path, _schema)
    return path


def _parse_detalhes(bruto: Any) -> dict[str, Any]:
    if isinstance(bruto, dict):
        return dict(bruto)
    if not bruto:
        return {}
    try:
        valor = json.loads(bruto)
    except json.JSONDecodeError:
        return {}
    return valor if isinstance(valor, dict) else {}


def _linha(
    session_id: str,
    id_: str,
    acao: str,
    detalhes: Any,
    status: str,
    criado_em: str,
    respondido_em: str | None,
) -> dict[str, Any]:
    return {
        "id": id_,
        "session_id": session_id,
        "acao": acao,
        "detalhes": _parse_detalhes(detalhes),
        "status": status,
        "criado_em": criado_em,
        "respondido_em": respondido_em,
    }


def registrar(
    session_id: str,
    id_: str,
    acao: str,
    detalhes: dict[str, Any],
    db_path: str | Path | None = None,
) -> None:
    """Idempotente em (session_id, id). Não sobrescreve status existente."""
    path = garantir_tabela(db_path)
    payload = json.dumps(detalhes, ensure_ascii=False, sort_keys=True)
    criado = _agora()

    def operation(connection: Any) -> None:
        connection.execute(
            f"INSERT INTO {TABELA}"
            "(session_id, id, acao, detalhes, status, criado_em, respondido_em) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL) "
            "ON CONFLICT(session_id, id) DO NOTHING",
            (session_id, id_, acao, payload, STATUS_PENDENTE, criado),
        )

    com_retry(path, operation)


def listar_pendentes(
    session_id: str, db_path: str | Path | None = None
) -> list[dict[str, Any]]:
    path = garantir_tabela(db_path)

    def operation(connection: Any) -> list[dict[str, Any]]:
        rows = connection.execute(
            f"SELECT session_id, id, acao, detalhes, status, criado_em, "
            f"respondido_em FROM {TABELA} "
            "WHERE session_id=? AND status=? ORDER BY criado_em, id",
            (session_id, STATUS_PENDENTE),
        ).fetchall()
        return [_linha(*row) for row in rows]

    return com_retry(path, operation)


def listar_todas(
    session_id: str, db_path: str | Path | None = None
) -> list[dict[str, Any]]:
    path = garantir_tabela(db_path)

    def operation(connection: Any) -> list[dict[str, Any]]:
        rows = connection.execute(
            f"SELECT session_id, id, acao, detalhes, status, criado_em, "
            f"respondido_em FROM {TABELA} "
            "WHERE session_id=? ORDER BY criado_em, id",
            (session_id,),
        ).fetchall()
        return [_linha(*row) for row in rows]

    return com_retry(path, operation)


def transicionar(
    session_id: str,
    id_: str,
    novo_status: str,
    db_path: str | Path | None = None,
) -> bool:
    """UPDATE condicional. True só se a linha estava pendente."""
    if novo_status not in (STATUS_APROVADA, STATUS_NEGADA, STATUS_CANCELADA):
        raise ValueError(f"status inválido: {novo_status}")
    path = garantir_tabela(db_path)
    quando = _agora()

    def operation(connection: Any) -> bool:
        connection.execute("BEGIN")
        cursor = connection.execute(
            f"UPDATE {TABELA} SET status=?, respondido_em=? "
            "WHERE session_id=? AND id=? AND status=?",
            (novo_status, quando, session_id, id_, STATUS_PENDENTE),
        )
        connection.commit()
        return cursor.rowcount == 1

    return com_retry(path, operation)


def cancelar_pendentes(
    session_id: str, db_path: str | Path | None = None
) -> list[dict[str, Any]]:
    path = garantir_tabela(db_path)
    quando = _agora()

    def operation(connection: Any) -> list[dict[str, Any]]:
        connection.execute("BEGIN")
        rows = connection.execute(
            f"SELECT session_id, id, acao, detalhes, status, criado_em, "
            f"respondido_em FROM {TABELA} "
            "WHERE session_id=? AND status=?",
            (session_id, STATUS_PENDENTE),
        ).fetchall()
        connection.execute(
            f"UPDATE {TABELA} SET status=?, respondido_em=? "
            "WHERE session_id=? AND status=?",
            (STATUS_CANCELADA, quando, session_id, STATUS_PENDENTE),
        )
        connection.commit()
        return [_linha(*row) for row in rows]

    return com_retry(path, operation)


def reverter_para_pendente(
    session_id: str, id_: str, db_path: str | Path | None = None
) -> bool:
    """Desfaz aprovada/negada após falha da retomada ADK. Evita estado preso."""
    path = garantir_tabela(db_path)

    def operation(connection: Any) -> bool:
        connection.execute("BEGIN")
        cursor = connection.execute(
            f"UPDATE {TABELA} SET status=?, respondido_em=NULL "
            "WHERE session_id=? AND id=? AND status IN (?, ?)",
            (
                STATUS_PENDENTE,
                session_id,
                id_,
                STATUS_APROVADA,
                STATUS_NEGADA,
            ),
        )
        connection.commit()
        return cursor.rowcount == 1

    return com_retry(path, operation)


def restaurar_pendencias(db_path: str | Path) -> None:
    """Apaga todas as pendências. A E20 chama junto com restaurar() do domínio."""
    path = garantir_tabela(db_path)

    def operation(connection: Any) -> None:
        connection.execute(f"DELETE FROM {TABELA}")

    com_retry(path, operation)
