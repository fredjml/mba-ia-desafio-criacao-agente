"""Repositório da tabela visitantes (criada pela E14).

Unicidade (apartamento, lower(nome), data) garantida por índice único
antes de gravar. IntegrityError → ja_autorizado (idempotente).
Conexão por operação via com_retry. Funções de repositório LEVANTAM.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from aurora.dados.dominio import (
    TABELA_VISITANTES,
    caminho_banco,
    com_retry,
    garantir_banco,
)

INDICE_UNICIDADE = "ux_visitantes_apt_nome_data"


def _garantir_indice(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {INDICE_UNICIDADE} "
        f"ON {TABELA_VISITANTES}(apartamento, lower(nome), data)"
    )


def gravar_visitante(
    db_path: str | Any,
    apartamento: str,
    nome: str,
    data: str,
) -> dict[str, str]:
    """Insere visitante. Idempotente: disputa/reexecução → ja_autorizado."""
    garantir_banco(db_path)

    def operation(connection: sqlite3.Connection) -> dict[str, str]:
        _garantir_indice(connection)
        connection.execute("BEGIN")
        try:
            connection.execute(
                f"INSERT INTO {TABELA_VISITANTES}(apartamento, nome, data) "
                "VALUES (?, ?, ?)",
                (apartamento, nome, data),
            )
        except sqlite3.IntegrityError:
            connection.rollback()
            return {"status": "ja_autorizado", "nome": nome, "data": data}
        connection.commit()
        return {"status": "autorizado", "nome": nome, "data": data}

    return com_retry(db_path, operation)


def listar_visitantes(db_path: str | Any, apartamento: str) -> list[dict[str, str]]:
    garantir_banco(db_path)

    def operation(connection: sqlite3.Connection) -> list[dict[str, str]]:
        rows = connection.execute(
            f"SELECT nome, data FROM {TABELA_VISITANTES} "
            "WHERE apartamento=? ORDER BY data, nome",
            (apartamento,),
        ).fetchall()
        return [{"nome": str(row[0]), "data": str(row[1])} for row in rows]

    return com_retry(db_path, operation)


def listar_visitantes_do_apartamento(
    apartamento: str,
    db_path: str | None = None,
) -> list[dict[str, str]]:
    """Repositório não-tool (E19): formato de GET /apartamentos/{n}/visitantes.

    Em falha de infraestrutura LEVANTA — a rota responde erro de servidor.
    """
    return listar_visitantes(caminho_banco(db_path), str(apartamento))
