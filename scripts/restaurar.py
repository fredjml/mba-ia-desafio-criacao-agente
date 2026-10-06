"""Restaura reservas, visitantes e pendências ao estado de dados/.

Uso: uv run python scripts/restaurar.py [--sessoes]
Sem --sessoes o banco de sessões é preservado; com ela, é apagado (API parada).
A sequência de códigos de reserva não regride: código nunca se repete.
"""

from __future__ import annotations

import argparse
import sys

from aurora.agentes.modelo import carregar_ambiente
from aurora.dados.dominio import caminho_banco, listar_ativas, restaurar
from aurora.dados.pendencias import restaurar_pendencias
from aurora.runtime.fabrica import caminho_banco_sessoes


def main() -> int:
    parser = argparse.ArgumentParser(prog="scripts/restaurar.py")
    parser.add_argument("--sessoes", action="store_true", help="também apaga o banco de sessões")
    args = parser.parse_args()

    # Só para ler os caminhos AURORA_* do .env; a chave não é necessária aqui.
    try:
        carregar_ambiente()
    except RuntimeError:
        pass

    dominio = caminho_banco()
    restaurar(dominio)
    restaurar_pendencias(dominio)
    print(f"restaurado: {dominio}")
    print(f"101: {listar_ativas(dominio, '101')}")

    if args.sessoes:
        sessoes = caminho_banco_sessoes()
        for sufixo in ("", "-wal", "-shm"):
            alvo = sessoes.with_name(sessoes.name + sufixo)
            try:
                alvo.unlink(missing_ok=True)
            except PermissionError:
                print(f"erro: {alvo} em uso; pare a API e repita", file=sys.stderr)
                return 1
        print(f"sessões apagadas: {sessoes}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
