"""Tabela própria de pendências de confirmação (D5).

Mora no MESMO SQLite do domínio (`caminho_banco()`). Schema sob demanda,
sem alterar dominio.py. busy_timeout e retry via `com_retry`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aurora.dados.dominio import caminho_banco, com_retry, garantir_banco

TABELA = "pendencias"
STATUS_PENDENTE = "pendente"
STATUS_PROCESSANDO = "processando"
STATUS_APROVADA = "aprovada"
STATUS_NEGADA = "negada"
STATUS_CANCELADA = "cancelada"
STATUS_VALIDOS = (
    STATUS_PENDENTE,
    STATUS_PROCESSANDO,
    STATUS_APROVADA,
    STATUS_NEGADA,
    STATUS_CANCELADA,
)


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat()


def _schema(connection: Any) -> None:
    # Não há banco persistente legado; o CREATE novo basta, sem migração.
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {TABELA}("
        "session_id TEXT NOT NULL,"
        "id TEXT NOT NULL,"
        "acao TEXT NOT NULL,"
        "detalhes TEXT NOT NULL,"
        "status TEXT NOT NULL CHECK ("
        "status IN ('pendente','processando','aprovada','negada','cancelada')),"
        "decisao TEXT CHECK (decisao IN ('aprovada','negada')),"
        "criado_em TEXT NOT NULL,"
        "iniciado_em TEXT,"
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
    decisao: str | None,
    criado_em: str,
    iniciado_em: str | None,
    respondido_em: str | None,
) -> dict[str, Any]:
    return {
        "id": id_,
        "session_id": session_id,
        "acao": acao,
        "detalhes": _parse_detalhes(detalhes),
        "status": status,
        "decisao": decisao,
        "criado_em": criado_em,
        "iniciado_em": iniciado_em,
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
            "(session_id, id, acao, detalhes, status, decisao, criado_em, "
            "iniciado_em, respondido_em) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?, NULL, NULL) "
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
            f"SELECT session_id, id, acao, detalhes, status, decisao, criado_em, "
            f"iniciado_em, respondido_em FROM {TABELA} "
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
            f"SELECT session_id, id, acao, detalhes, status, decisao, criado_em, "
            f"iniciado_em, respondido_em FROM {TABELA} "
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


def iniciar_processamento(
    session_id: str,
    id_: str,
    decisao: str,
    db_path: str | Path | None = None,
) -> bool:
    """Reserva atomicamente uma pendência para a retomada ADK."""
    if decisao not in (STATUS_APROVADA, STATUS_NEGADA):
        raise ValueError(f"decisão inválida: {decisao}")
    path = garantir_tabela(db_path)
    quando = _agora()

    def operation(connection: Any) -> bool:
        connection.execute("BEGIN")
        cursor = connection.execute(
            f"UPDATE {TABELA} SET status=?, decisao=?, iniciado_em=?, "
            "respondido_em=NULL WHERE session_id=? AND id=? AND status=?",
            (
                STATUS_PROCESSANDO,
                decisao,
                quando,
                session_id,
                id_,
                STATUS_PENDENTE,
            ),
        )
        connection.commit()
        return cursor.rowcount == 1

    return com_retry(path, operation)


def finalizar_processamento(
    session_id: str,
    id_: str,
    db_path: str | Path | None = None,
) -> bool:
    """Consolida a decisão gravada somente após a retomada terminar."""
    path = garantir_tabela(db_path)
    quando = _agora()

    def operation(connection: Any) -> bool:
        connection.execute("BEGIN")
        cursor = connection.execute(
            f"UPDATE {TABELA} SET status=decisao, respondido_em=? "
            "WHERE session_id=? AND id=? AND status=? "
            "AND decisao IN (?, ?)",
            (
                quando,
                session_id,
                id_,
                STATUS_PROCESSANDO,
                STATUS_APROVADA,
                STATUS_NEGADA,
            ),
        )
        connection.commit()
        return cursor.rowcount == 1

    return com_retry(path, operation)


def recuperar_processando(
    session_id: str | None = None,
    *,
    mais_velho_que_s: float | None = None,
    db_path: str | Path | None = None,
) -> int:
    """Recupera todos na partida, ou só antigos de uma sessão durante o uso."""
    path = garantir_tabela(db_path)
    limite = (
        (datetime.now(timezone.utc) - timedelta(seconds=mais_velho_que_s)).isoformat()
        if mais_velho_que_s is not None
        else None
    )

    def operation(connection: Any) -> int:
        filtros = ["status=?"]
        parametros: list[Any] = [STATUS_PROCESSANDO]
        if session_id is not None:
            filtros.append("session_id=?")
            parametros.append(session_id)
        if limite is not None:
            filtros.append("iniciado_em<?")
            parametros.append(limite)
        connection.execute("BEGIN")
        cursor = connection.execute(
            f"UPDATE {TABELA} SET status=?, decisao=NULL, iniciado_em=NULL, "
            f"respondido_em=NULL WHERE {' AND '.join(filtros)}",
            (STATUS_PENDENTE, *parametros),
        )
        connection.commit()
        return cursor.rowcount

    return com_retry(path, operation)


def cancelar_pendentes(
    session_id: str, db_path: str | Path | None = None
) -> list[dict[str, Any]]:
    path = garantir_tabela(db_path)
    quando = _agora()

    def operation(connection: Any) -> list[dict[str, Any]]:
        connection.execute("BEGIN")
        rows = connection.execute(
            f"SELECT session_id, id, acao, detalhes, status, decisao, criado_em, "
            f"iniciado_em, respondido_em FROM {TABELA} "
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
    """Desfaz processando após qualquer falha/cancelamento da retomada ADK."""
    path = garantir_tabela(db_path)

    def operation(connection: Any) -> bool:
        connection.execute("BEGIN")
        cursor = connection.execute(
            f"UPDATE {TABELA} SET status=?, decisao=NULL, iniciado_em=NULL, "
            "respondido_em=NULL WHERE session_id=? AND id=? AND status=?",
            (
                STATUS_PENDENTE,
                session_id,
                id_,
                STATUS_PROCESSANDO,
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
