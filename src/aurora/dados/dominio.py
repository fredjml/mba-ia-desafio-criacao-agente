"""Repositório SQLite do domínio (reservas, visitantes, sequências).

Origem da política de gravação (estratégia E1, E10):
spike_concorrencia/spike_concorrencia.py
(ReservationStore.unique_insert, transaction_with_retry, initialize_database).
Copiado e adaptado; este módulo NÃO importa os spikes.

Banco separado do de sessões do ADK. Caminho: parâmetro, AURORA_DOMAIN_DB
ou var/dominio.sqlite3 na raiz do projeto (E21 trata o .gitignore).
Conexão por operação, WAL, busy_timeout de alguns segundos, retry limitado.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

from aurora.dados.carregar import carregar_reservas, carregar_visitantes

VAR_BANCO_DOMINIO = "AURORA_DOMAIN_DB"
NOME_SEQUENCIA_RESERVAS = "reservas"
TABELA_RESERVAS = "reservas"
TABELA_VISITANTES = "visitantes"
TABELA_SEQUENCIAS = "code_sequences"

MAX_RETRIES_LOCK = 80
BUSY_TIMEOUT_MS = 5000

T = TypeVar("T")


def _raiz_projeto() -> Path:
    for start in (Path(__file__).resolve().parent, Path.cwd()):
        for candidate in (start, *start.parents):
            if (candidate / "pyproject.toml").is_file():
                return candidate
    return Path(__file__).resolve().parents[3]


def caminho_banco(explicito: str | os.PathLike[str] | None = None) -> Path:
    if explicito is not None:
        return Path(explicito)
    env = os.environ.get(VAR_BANCO_DOMINIO)
    if env:
        return Path(env)
    return _raiz_projeto() / "var" / "dominio.sqlite3"


def _numero_codigo(codigo: str) -> int:
    return int(str(codigo).rsplit("-", 1)[-1])


def _esta_locked(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()


def _conectar(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        str(db_path), timeout=BUSY_TIMEOUT_MS / 1000.0, isolation_level=None
    )
    connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return connection


def com_retry(
    db_path: str | os.PathLike[str],
    operation: Callable[[sqlite3.Connection], T],
) -> T:
    """Uma conexão por tentativa; retry limitado com backoff se locked."""
    last_error: BaseException | None = None
    path = Path(db_path)
    for attempt in range(MAX_RETRIES_LOCK + 1):
        connection = _conectar(path)
        try:
            return operation(connection)
        except BaseException as exc:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            if _esta_locked(exc) and attempt < MAX_RETRIES_LOCK:
                last_error = exc
                time.sleep(min(0.0005 * (attempt + 1), 0.010))
                continue
            raise
        finally:
            connection.close()
    raise sqlite3.OperationalError(str(last_error or "database is locked"))


def _maior_codigo_semente() -> int:
    reservas = carregar_reservas()
    return max(_numero_codigo(str(item["codigo"])) for item in reservas)


def _aplicar_schema(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {TABELA_RESERVAS}("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "codigo TEXT NOT NULL UNIQUE,"
        "apartamento TEXT NOT NULL,"
        "area TEXT NOT NULL,"
        "data TEXT NOT NULL,"
        "ativa INTEGER NOT NULL CHECK (ativa IN (0, 1)))"
    )
    connection.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS ux_reservas_ativa "
        f"ON {TABELA_RESERVAS}(area, data) WHERE ativa=1"
    )
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {TABELA_VISITANTES}("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "apartamento TEXT NOT NULL,"
        "nome TEXT NOT NULL,"
        "data TEXT NOT NULL)"
    )
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {TABELA_SEQUENCIAS}("
        "name TEXT PRIMARY KEY,"
        "value INTEGER NOT NULL)"
    )


def _semear_se_vazio(connection: sqlite3.Connection) -> None:
    reservas = carregar_reservas()
    visitantes = carregar_visitantes()
    if connection.execute(f"SELECT COUNT(*) FROM {TABELA_RESERVAS}").fetchone()[0] == 0:
        connection.executemany(
            f"INSERT INTO {TABELA_RESERVAS}"
            "(codigo, apartamento, area, data, ativa) VALUES (?, ?, ?, ?, 1)",
            [
                (item["codigo"], item["apartamento"], item["area"], item["data"])
                for item in reservas
            ],
        )
    if connection.execute(f"SELECT COUNT(*) FROM {TABELA_VISITANTES}").fetchone()[0] == 0:
        connection.executemany(
            f"INSERT INTO {TABELA_VISITANTES}(apartamento, nome, data) VALUES (?, ?, ?)",
            [
                (item["apartamento"], item["nome"], item["data"])
                for item in visitantes
            ],
        )
    maximo = _maior_codigo_semente()
    atual = connection.execute(
        f"SELECT value FROM {TABELA_SEQUENCIAS} WHERE name=?",
        (NOME_SEQUENCIA_RESERVAS,),
    ).fetchone()
    if atual is None:
        connection.execute(
            f"INSERT INTO {TABELA_SEQUENCIAS}(name, value) VALUES (?, ?)",
            (NOME_SEQUENCIA_RESERVAS, maximo),
        )
    elif int(atual[0]) < maximo:
        connection.execute(
            f"UPDATE {TABELA_SEQUENCIAS} SET value=? WHERE name=?",
            (maximo, NOME_SEQUENCIA_RESERVAS),
        )


def garantir_banco(db_path: str | os.PathLike[str] | None = None) -> Path:
    path = caminho_banco(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    def operation(connection: sqlite3.Connection) -> None:
        _aplicar_schema(connection)
        _semear_se_vazio(connection)

    com_retry(path, operation)
    return path


def restaurar(db_path: str | os.PathLike[str]) -> None:
    """Devolve reservas e visitantes ao estado de dados/. Sequência não regride."""
    path = garantir_banco(db_path)
    reservas = carregar_reservas()
    visitantes = carregar_visitantes()
    maximo = _maior_codigo_semente()

    def operation(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN")
        connection.execute(f"DELETE FROM {TABELA_RESERVAS}")
        connection.execute(f"DELETE FROM {TABELA_VISITANTES}")
        connection.executemany(
            f"INSERT INTO {TABELA_RESERVAS}"
            "(codigo, apartamento, area, data, ativa) VALUES (?, ?, ?, ?, 1)",
            [
                (item["codigo"], item["apartamento"], item["area"], item["data"])
                for item in reservas
            ],
        )
        connection.executemany(
            f"INSERT INTO {TABELA_VISITANTES}(apartamento, nome, data) VALUES (?, ?, ?)",
            [
                (item["apartamento"], item["nome"], item["data"])
                for item in visitantes
            ],
        )
        atual = connection.execute(
            f"SELECT value FROM {TABELA_SEQUENCIAS} WHERE name=?",
            (NOME_SEQUENCIA_RESERVAS,),
        ).fetchone()
        if atual is None or int(atual[0]) < maximo:
            connection.execute(
                f"INSERT INTO {TABELA_SEQUENCIAS}(name, value) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (NOME_SEQUENCIA_RESERVAS, maximo),
            )
        connection.commit()

    com_retry(path, operation)


def _proximo_codigo(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        f"UPDATE {TABELA_SEQUENCIAS} SET value=value+1 WHERE name=? RETURNING value",
        (NOME_SEQUENCIA_RESERVAS,),
    ).fetchone()
    if row is None:
        raise sqlite3.DatabaseError("sequência de reservas ausente")
    return f"RSV-{row[0]}"


def disponivel(db_path: str | os.PathLike[str], area: str, data: str) -> bool:
    garantir_banco(db_path)

    def operation(connection: sqlite3.Connection) -> bool:
        row = connection.execute(
            f"SELECT 1 FROM {TABELA_RESERVAS} WHERE area=? AND data=? AND ativa=1",
            (area, data),
        ).fetchone()
        return row is None

    return com_retry(db_path, operation)


def listar_ativas(
    db_path: str | os.PathLike[str], apartamento: str
) -> list[dict[str, str]]:
    garantir_banco(db_path)

    def operation(connection: sqlite3.Connection) -> list[dict[str, str]]:
        rows = connection.execute(
            f"SELECT codigo, area, data FROM {TABELA_RESERVAS} "
            "WHERE apartamento=? AND ativa=1 ORDER BY data, codigo",
            (apartamento,),
        ).fetchall()
        return [
            {"codigo": str(row[0]), "area": str(row[1]), "data": str(row[2])}
            for row in rows
        ]

    return com_retry(db_path, operation)


def gravar_reserva(
    db_path: str | os.PathLike[str],
    apartamento: str,
    area: str,
    data: str,
) -> dict[str, str]:
    """E1: UNIQUE parcial + IntegrityError → recusada; mesmo apt → ja_reservada.

    Origem: spike_concorrencia/spike_concorrencia.py ReservationStore.unique_insert.
    """
    garantir_banco(db_path)

    def operation(connection: sqlite3.Connection) -> dict[str, str]:
        connection.execute("BEGIN")
        mesma = connection.execute(
            f"SELECT codigo FROM {TABELA_RESERVAS} "
            "WHERE apartamento=? AND area=? AND data=? AND ativa=1",
            (apartamento, area, data),
        ).fetchone()
        if mesma:
            connection.commit()
            return {"status": "ja_reservada", "codigo": str(mesma[0])}
        codigo = _proximo_codigo(connection)
        try:
            connection.execute(
                f"INSERT INTO {TABELA_RESERVAS}"
                "(codigo, apartamento, area, data, ativa) VALUES (?, ?, ?, ?, 1)",
                (codigo, apartamento, area, data),
            )
        except sqlite3.IntegrityError:
            connection.rollback()
            mesma_depois = connection.execute(
                f"SELECT codigo FROM {TABELA_RESERVAS} "
                "WHERE apartamento=? AND area=? AND data=? AND ativa=1",
                (apartamento, area, data),
            ).fetchone()
            if mesma_depois:
                return {"status": "ja_reservada", "codigo": str(mesma_depois[0])}
            return {"status": "recusada"}
        connection.commit()
        return {"status": "reservada", "codigo": codigo}

    return com_retry(db_path, operation)


def cancelar_no_banco(
    db_path: str | os.PathLike[str],
    apartamento: str,
    codigo: str,
) -> dict[str, str]:
    garantir_banco(db_path)

    def operation(connection: sqlite3.Connection) -> dict[str, str]:
        connection.execute("BEGIN")
        row = connection.execute(
            f"SELECT apartamento, ativa FROM {TABELA_RESERVAS} WHERE codigo=?",
            (codigo,),
        ).fetchone()
        if row is None or str(row[0]) != apartamento:
            connection.commit()
            return {"status": "nao_encontrada"}
        if int(row[1]) == 0:
            connection.commit()
            return {"status": "ja_cancelada"}
        connection.execute(
            f"UPDATE {TABELA_RESERVAS} SET ativa=0 WHERE codigo=?",
            (codigo,),
        )
        connection.commit()
        return {"status": "cancelada", "codigo": codigo}

    return com_retry(db_path, operation)
