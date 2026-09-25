"""Carregamento somente-leitura dos JSON em dados/ (UTF-8).

VALIDAÇÃO (escrita antes da implementação):
- `python -m aurora.dados.carregar --verificar` sai 0 se, e só se:
  1. os quatro JSON existem e são listas de objetos com as chaves mínimas;
  2. o apartamento 101 tem a reserva RSV-1377;
  3. o apartamento 302 tem o visitante Marina Duarte.
- Imprime as contagens (apartamentos, áreas, reservas, visitantes).
- Não carrega regulamento.md (E16).
- Nunca escreve em dados/.
- Diretório: raiz do projeto / dados, ou AURORA_DADOS_DIR.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

CHAVES: dict[str, tuple[str, ...]] = {
    "apartamentos": ("numero", "morador"),
    "areas": ("id", "nome", "taxa"),
    "reservas": ("codigo", "apartamento", "area", "data"),
    "visitantes": ("apartamento", "nome", "data"),
}

ARQUIVOS: dict[str, str] = {
    "apartamentos": "apartamentos.json",
    "areas": "areas.json",
    "reservas": "reservas.json",
    "visitantes": "visitantes.json",
}


def diretorio_dados(override: str | os.PathLike[str] | None = None) -> Path:
    if override is not None:
        return Path(override)
    env = os.environ.get("AURORA_DADOS_DIR")
    if env:
        return Path(env)
    return _raiz_projeto() / "dados"


def _raiz_projeto() -> Path:
    for start in (Path(__file__).resolve().parent, Path.cwd()):
        for candidate in (start, *start.parents):
            if (candidate / "pyproject.toml").is_file():
                return candidate
    return Path(__file__).resolve().parents[3]


def _ler_json(caminho: Path) -> Any:
    with caminho.open(encoding="utf-8") as stream:
        return json.load(stream)


def _validar_lista(nome: str, bruto: Any) -> list[dict[str, Any]]:
    if not isinstance(bruto, list):
        raise ValueError(f"{nome}: esperado lista JSON, obtido {type(bruto).__name__}")
    esperadas = CHAVES[nome]
    itens: list[dict[str, Any]] = []
    for indice, item in enumerate(bruto):
        if not isinstance(item, dict):
            raise ValueError(f"{nome}[{indice}]: esperado objeto")
        faltando = [chave for chave in esperadas if chave not in item]
        if faltando:
            raise ValueError(f"{nome}[{indice}]: chaves ausentes {faltando}")
        itens.append(item)
    return itens


def _carregar_tabela(
    nome: str,
    diretorio: Path,
) -> list[dict[str, Any]]:
    caminho = diretorio / ARQUIVOS[nome]
    if not caminho.is_file():
        raise FileNotFoundError(f"arquivo ausente: {caminho}")
    return _validar_lista(nome, _ler_json(caminho))


def carregar_apartamentos(
    diretorio: str | os.PathLike[str] | None = None,
) -> list[dict[str, Any]]:
    return _carregar_tabela("apartamentos", diretorio_dados(diretorio))


def carregar_areas(
    diretorio: str | os.PathLike[str] | None = None,
) -> list[dict[str, Any]]:
    return _carregar_tabela("areas", diretorio_dados(diretorio))


def carregar_reservas(
    diretorio: str | os.PathLike[str] | None = None,
) -> list[dict[str, Any]]:
    return _carregar_tabela("reservas", diretorio_dados(diretorio))


def carregar_visitantes(
    diretorio: str | os.PathLike[str] | None = None,
) -> list[dict[str, Any]]:
    return _carregar_tabela("visitantes", diretorio_dados(diretorio))


def verificar(diretorio: str | os.PathLike[str] | None = None) -> int:
    pasta = diretorio_dados(diretorio)
    apartamentos = carregar_apartamentos(pasta)
    areas = carregar_areas(pasta)
    reservas = carregar_reservas(pasta)
    visitantes = carregar_visitantes(pasta)

    print(
        "contagem "
        f"apartamentos={len(apartamentos)} "
        f"areas={len(areas)} "
        f"reservas={len(reservas)} "
        f"visitantes={len(visitantes)}"
    )

    tem_rsv = any(
        item.get("apartamento") == "101" and item.get("codigo") == "RSV-1377"
        for item in reservas
    )
    tem_marina = any(
        item.get("apartamento") == "302" and item.get("nome") == "Marina Duarte"
        for item in visitantes
    )
    print(
        "fato apartamento=101 reserva=RSV-1377 "
        f"{'presente' if tem_rsv else 'ausente'}"
    )
    print(
        "fato apartamento=302 visitante=Marina Duarte "
        f"{'presente' if tem_marina else 'ausente'}"
    )
    if tem_rsv and tem_marina:
        print("verificar=ok")
        return 0
    print("verificar=falha")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m aurora.dados.carregar")
    parser.add_argument(
        "--verificar",
        action="store_true",
        help="imprime contagens e confirma os dois fatos do avaliador",
    )
    args = parser.parse_args(argv)
    if not args.verificar:
        parser.print_help()
        return 2
    return verificar()


if __name__ == "__main__":
    sys.exit(main())
