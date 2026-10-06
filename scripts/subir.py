"""Sobe a API em http://localhost:8000 (Ctrl+C para parar). Não restaura dados.

Uso: uv run python scripts/subir.py
"""

from __future__ import annotations

import uvicorn


def main() -> None:
    uvicorn.run("aurora.api.app:app", host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
