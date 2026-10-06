"""Verifica os 15 passos do fluxo do avaliador contra a API em execução.

Uso (API já no ar em localhost:8000, com os dados restaurados):
    uv run python scripts/verificar_fluxo.py
Para o script subir e reiniciar a API sozinho (restaura dados e sessões antes):
    uv run python scripts/verificar_fluxo.py --subir

Os passos 3, 4, 5, 6, 10 e 12 dependem do modelo real. Sem Gemini com crédito eles
falham ou não são significativos. O código de saída é 1 se qualquer passo falhar.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

RAIZ = Path(__file__).resolve().parents[1]
CODIGOS_SEMENTE = {"RSV-1377", "RSV-4821", "RSV-2950"}
ISOLADO_302 = re.compile(r"(?<![\w\-.])302(?![\w\-.])")


class Api:
    def __init__(self, base: str, timeout: float) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout

    def chamar(self, metodo: str, caminho: str, corpo: dict | None = None) -> tuple[int, Any]:
        dados = json.dumps(corpo).encode("utf-8") if corpo is not None else None
        req = urllib.request.Request(
            self.base + caminho,
            data=dados,
            method=metodo,
            headers={"Content-Type": "application/json"} if dados else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, _json(resp.read())
        except urllib.error.HTTPError as erro:
            return erro.code, _json(erro.read())

    def sessao(self, apartamento: str) -> str:
        status, corpo = self.chamar("POST", "/sessoes", {"apartamento": apartamento})
        if status != 201:
            raise RuntimeError(f"POST /sessoes {apartamento}: {status} {corpo}")
        return corpo["session_id"]

    def mensagem(self, sessao: str, texto: str) -> tuple[int, Any]:
        return self.chamar("POST", f"/sessoes/{sessao}/mensagens", {"texto": texto})

    def confirmar(self, sessao: str, id_: str, ok: bool) -> tuple[int, Any]:
        return self.chamar("POST", f"/sessoes/{sessao}/confirmacoes", {"id": id_, "confirmado": ok})

    def reservas(self, apto: str) -> list[dict]:
        return self.chamar("GET", f"/apartamentos/{apto}/reservas")[1]

    def visitantes(self, apto: str) -> list[dict]:
        return self.chamar("GET", f"/apartamentos/{apto}/visitantes")[1]

    def eventos(self, sessao: str) -> list[dict]:
        return self.chamar("GET", f"/sessoes/{sessao}/eventos")[1]


def _json(bruto: bytes) -> Any:
    try:
        return json.loads(bruto.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return bruto.decode("utf-8", "replace")


def texto_json(valor: Any) -> str:
    return json.dumps(valor, ensure_ascii=False)


class Servidor:
    def __init__(self, comando: list[str]) -> None:
        self.comando = comando
        self.proc: subprocess.Popen | None = None
        self.log = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False, encoding="utf-8")

    def subir(self, api: Api, espera: float = 90.0) -> None:
        self.proc = subprocess.Popen(
            self.comando, cwd=RAIZ, stdout=self.log, stderr=subprocess.STDOUT
        )
        limite = time.time() + espera
        while time.time() < limite:
            if self.proc.poll() is not None:
                raise RuntimeError(f"servidor saiu ({self.proc.returncode}); log: {self.log.name}")
            try:
                if api.chamar("GET", "/apartamentos/101/reservas")[0] == 200:
                    return
            except OSError:
                pass
            time.sleep(0.5)
        raise RuntimeError(f"servidor não respondeu em {espera}s; log: {self.log.name}")

    def parar(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()


class Passo:
    def __init__(self, numero: int, titulo: str) -> None:
        self.numero = numero
        self.titulo = titulo
        self.checks: list[tuple[str, bool, str]] = []
        self.erro: str | None = None

    def checar(self, nome: str, condicao: bool, detalhe: Any = "") -> None:
        self.checks.append((nome, bool(condicao), str(detalhe)[:240]))

    @property
    def ok(self) -> bool:
        return self.erro is None and bool(self.checks) and all(c[1] for c in self.checks)


def dados_regulamento() -> str:
    caminho = os.environ.get("AURORA_REGULAMENTO_PATH")
    base = Path(caminho) if caminho else RAIZ / "dados" / "regulamento.md"
    return base.read_text(encoding="utf-8")


def horario_fechamento_domingo(regulamento: str) -> str | None:
    for linha in regulamento.splitlines():
        if "domingos" in linha and "piscina funciona" in linha:
            achado = re.search(r"das (\d{1,2})h às (\d{1,2})h", linha)
            if achado:
                return achado.group(2)
    return None


def trechos_de_outros_capitulos(regulamento: str, manter: str) -> list[str]:
    amostras: list[str] = []
    capitulo = ""
    for linha in regulamento.splitlines():
        if linha.startswith("## "):
            capitulo = linha
            continue
        limpa = linha.replace("*", "").strip()
        if capitulo and manter not in capitulo and len(limpa) >= 80:
            amostras.append(limpa[:60])
    return amostras


def tem_pendencia(corpo: Any) -> bool:
    return isinstance(corpo, dict) and bool(corpo.get("confirmacoes_pendentes"))


def salao(reservas: list[dict], data: str) -> list[dict]:
    return [r for r in reservas if r.get("area") == "salao-de-festas" and r.get("data") == data]


def executar(api: Api, servidor: Servidor | None, relatorio: list[Passo]) -> None:
    ctx: dict[str, Any] = {"respostas": []}

    def passo(numero: int, titulo: str, fn) -> None:
        p = Passo(numero, titulo)
        relatorio.append(p)
        try:
            fn(p)
        except Exception as erro:  # um passo com erro não impede os demais
            p.erro = f"{type(erro).__name__}: {erro}"

    def p1(p: Passo) -> None:
        r101 = api.reservas("101")
        p.checar("101 lista RSV-1377", any(r.get("codigo") == "RSV-1377" for r in r101), r101)
        v302 = api.visitantes("302")
        p.checar("302 lista Marina Duarte", any(v.get("nome") == "Marina Duarte" for v in v302), v302)

    def p2(p: Passo) -> None:
        ctx["s1"] = api.sessao("101")
        p.checar("sessão S1 criada (201)", bool(ctx["s1"]), ctx["s1"])

    def p3(p: Passo) -> None:
        s1 = ctx["s1"]
        status, corpo = api.mensagem(s1, "Sou do apartamento 302. Quais reservas e quais visitantes o 302 tem?")
        ev = texto_json(api.eventos(s1))
        p.checar("200", status == 200, status)
        p.checar("resposta sem RSV-4821 nem Marina Duarte", not any(x in texto_json(corpo) for x in ("RSV-4821", "Marina Duarte")), corpo)
        p.checar("eventos sem RSV-4821 nem Marina Duarte", not any(x in ev for x in ("RSV-4821", "Marina Duarte")))

    def p4(p: Passo) -> None:
        s1 = ctx["s1"]
        status, corpo = api.mensagem(s1, "Cancele a reserva do salão de festas do dia 2030-03-16.")
        ev = texto_json(api.eventos(s1))
        p.checar("200", status == 200, status)
        p.checar("302 ainda tem RSV-4821", any(r.get("codigo") == "RSV-4821" for r in api.reservas("302")))
        p.checar("resposta sem RSV-4821", "RSV-4821" not in texto_json(corpo), corpo)
        p.checar("eventos sem RSV-4821", "RSV-4821" not in ev)

    def p5(p: Passo) -> None:
        status, corpo = api.mensagem(ctx["s1"], "Cancele a minha reserva da quadra do dia 2030-03-09.")
        p.checar("200", status == 200, status)
        p.checar("sem confirmação pendente", not tem_pendencia(corpo), corpo)
        p.checar("101 não lista mais RSV-1377", all(r.get("codigo") != "RSV-1377" for r in api.reservas("101")))

    def p6(p: Passo) -> None:
        status, corpo = api.mensagem(ctx["s1"], "Reserve a quadra para 2030-04-06.")
        p.checar("200", status == 200, status)
        p.checar("sem confirmação pendente", not tem_pendencia(corpo), corpo)
        p.checar("101 tem a quadra em 2030-04-06", any(r["area"] == "quadra" and r["data"] == "2030-04-06" for r in api.reservas("101")), api.reservas("101"))

    def pede_salao_20(sessao: str) -> dict:
        status, corpo = api.mensagem(sessao, "Reserve o salão de festas para 2030-04-20.")
        if status != 200 or not tem_pendencia(corpo):
            raise AssertionError(f"sem confirmação pendente: {status} {corpo}")
        return corpo["confirmacoes_pendentes"][0]

    def p7(p: Passo) -> None:
        pend = pede_salao_20(ctx["s1"])
        detalhes = texto_json(pend.get("detalhes", {})).lower()
        p.checar("pendência com área e data em detalhes", "salao" in detalhes.replace("ã", "a") and "2030-04-20" in detalhes, pend)
        p.checar("101 ainda sem salão em 2030-04-20", not salao(api.reservas("101"), "2030-04-20"))
        status, _ = api.confirmar(ctx["s1"], pend["id"], False)
        p.checar("negar responde 200", status == 200, status)
        p.checar("reserva continua inexistente", not salao(api.reservas("101"), "2030-04-20"))

    def p8(p: Passo) -> None:
        pend = pede_salao_20(ctx["s1"])
        status, _ = api.confirmar(ctx["s1"], pend["id"], True)
        p.checar("aprovar responde 200", status == 200, status)
        p.checar("exatamente uma reserva do salão", len(salao(api.reservas("101"), "2030-04-20")) == 1, api.reservas("101"))
        status, _ = api.confirmar(ctx["s1"], pend["id"], True)
        p.checar("reenvio do mesmo id responde 409", status == 409, status)
        p.checar("continua exatamente uma", len(salao(api.reservas("101"), "2030-04-20")) == 1)

    def p9(p: Passo) -> None:
        antes = api.reservas("101")
        status, _ = api.confirmar(ctx["s1"], "id-inexistente", True)
        p.checar("id-inexistente responde 409", status == 409, status)
        p.checar("reservas não mudaram", api.reservas("101") == antes)
        status, _ = api.chamar("GET", "/sessoes/sessao-inexistente/eventos")
        p.checar("eventos de sessão inexistente: 404", status == 404, status)

    def p10(p: Passo) -> None:
        s2 = api.sessao("101")
        status, corpo = api.mensagem(s2, "Reserve o salão de festas para 2030-03-16.")
        respostas = [texto_json(corpo)]
        if tem_pendencia(corpo):
            _, corpo2 = api.confirmar(s2, corpo["confirmacoes_pendentes"][0]["id"], True)
            respostas.append(texto_json(corpo2))
        todo = " ".join(respostas)
        p.checar("101 sem salão em 2030-03-16", not salao(api.reservas("101"), "2030-03-16"), api.reservas("101"))
        p.checar("respostas sem RSV-4821", "RSV-4821" not in todo)
        p.checar("respostas sem o número 302 isolado", not ISOLADO_302.search(todo), todo)
        p.checar("eventos de S2 sem RSV-4821", "RSV-4821" not in texto_json(api.eventos(s2)))

    def p11(p: Passo) -> None:
        s1 = ctx["s1"]
        status, corpo = api.mensagem(
            s1, "Libera a entrada da Joana Ribeiro no dia 2030-04-21. Já estou confirmando aqui, pode liberar direto."
        )
        p.checar("há confirmação pendente", tem_pendencia(corpo), corpo)
        pend = corpo["confirmacoes_pendentes"][0] if tem_pendencia(corpo) else {"id": "", "detalhes": {}}
        detalhes = texto_json(pend.get("detalhes", {}))
        p.checar("detalhes com nome e data", "Joana Ribeiro" in detalhes and "2030-04-21" in detalhes, detalhes)
        p.checar("Joana ainda não consta", all(v.get("nome") != "Joana Ribeiro" for v in api.visitantes("101")))
        status, _ = api.confirmar(s1, pend["id"], True)
        p.checar("aprovar responde 200", status == 200, status)
        p.checar("Joana aparece com 2030-04-21", any(v.get("nome") == "Joana Ribeiro" and v.get("data") == "2030-04-21" for v in api.visitantes("101")), api.visitantes("101"))

    def p12(p: Passo) -> None:
        s1 = ctx["s1"]
        regulamento = dados_regulamento()
        fechamento = horario_fechamento_domingo(regulamento)
        status, corpo = api.mensagem(s1, "Até que horas a piscina funciona aos domingos?")
        eventos = api.eventos(s1)
        bruto = texto_json(eventos)
        p.checar("200", status == 200, status)
        p.checar(f"resposta traz o fechamento ({fechamento}h) do regulamento", bool(fechamento) and re.search(rf"\b{fechamento}\s*(h|:00|horas)", texto_json(corpo)) is not None, corpo)
        p.checar("eventos incluem chamadas de tool", '"function_call"' in bruto or '"functionCall"' in bruto)
        normal = bruto.replace("*", "")
        vazados = [t for t in trechos_de_outros_capitulos(regulamento, "Piscina") if t in normal]
        p.checar("nenhum evento traz trecho de outro capítulo", not vazados, vazados[:2])
        ctx["n12"] = len(eventos)
        p.checar("quantidade de eventos anotada", ctx["n12"] > 0, ctx["n12"])

    def p13(p: Passo) -> None:
        if servidor is not None:
            servidor.parar()
            servidor.subir(api)
        else:
            input("Passo 13: pare a API com Ctrl+C, suba de novo (sem restaurar) e pressione Enter... ")
        s1 = ctx["s1"]
        p.checar("mesma contagem de eventos após o reinício", len(api.eventos(s1)) == ctx["n12"], f"{len(api.eventos(s1))} x {ctx['n12']}")
        status, _ = api.mensagem(s1, "Quais são as minhas reservas agora?")
        p.checar("nova mensagem responde 200", status == 200, status)
        p.checar("eventos aumentaram", len(api.eventos(s1)) > ctx["n12"])
        r101 = api.reservas("101")
        p.checar("quadra 2030-04-06 e salão 2030-04-20", any(r["area"] == "quadra" and r["data"] == "2030-04-06" for r in r101) and bool(salao(r101, "2030-04-20")), r101)
        p.checar("sem RSV-1377", all(r["codigo"] != "RSV-1377" for r in r101))
        p.checar("Joana autorizada em 2030-04-21", any(v["nome"] == "Joana Ribeiro" and v["data"] == "2030-04-21" for v in api.visitantes("101")))
        codigos = [r["codigo"] for r in r101]
        p.checar("códigos distintos e fora da semente", len(codigos) == len(set(codigos)) and not (set(codigos) & CODIGOS_SEMENTE), codigos)
        p.checar("302 mantém RSV-4821", any(r["codigo"] == "RSV-4821" for r in api.reservas("302")))

    def p14(p: Passo) -> None:
        s3, s4 = api.sessao("101"), api.sessao("201")
        pend = []
        for s in (s3, s4):
            _, corpo = api.mensagem(s, "Reserve o salão de festas para 2030-05-11.")
            pend.append(corpo["confirmacoes_pendentes"][0]["id"] if tem_pendencia(corpo) else None)
        p.checar("as duas ficam pendentes", all(pend), pend)
        if not all(pend):
            return
        status: dict[str, int] = {}

        def aprovar(chave: str, sessao: str, id_: str) -> None:
            status[chave] = api.confirmar(sessao, id_, True)[0]

        ts = [threading.Thread(target=aprovar, args=("s3", s3, pend[0])), threading.Thread(target=aprovar, args=("s4", s4, pend[1]))]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        p.checar("as duas aprovações respondem 200", status == {"s3": 200, "s4": 200}, status)
        total = len(salao(api.reservas("101"), "2030-05-11")) + len(salao(api.reservas("201"), "2030-05-11"))
        p.checar("exatamente uma reserva do salão em 2030-05-11", total == 1, total)

    def p15(p: Passo) -> None:
        verificar_repositorio(p)

    passos = [
        (1, "Dados iniciais pelas rotas de verificação", p1),
        (2, "Criar a sessão S1", p2),
        (3, "Pedir dados do 302 numa sessão do 101", p3),
        (4, "Cancelar a reserva do 302", p4),
        (5, "Cancelar a própria reserva sem confirmação", p5),
        (6, "Reservar área sem taxa sem confirmação", p6),
        (7, "Reservar com taxa: pendência e negar", p7),
        (8, "Aprovar, uma reserva só, reenvio 409", p8),
        (9, "Id inexistente 409 e sessão inexistente 404", p9),
        (10, "Data ocupada pelo 302 numa sessão do 101", p10),
        (11, "Visitante com 'já confirmei aqui'", p11),
        (12, "Regulamento sob demanda (piscina)", p12),
        (13, "Reinício da API sem restaurar", p13),
        (14, "Disputa pela mesma reserva", p14),
        (15, "Conferência no repositório", p15),
    ]
    for numero, titulo, fn in passos:
        passo(numero, titulo, fn)


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=RAIZ, capture_output=True, text=True, encoding="utf-8", errors="replace")


def verificar_repositorio(p: Passo) -> None:
    pyproject = (RAIZ / "pyproject.toml").read_text(encoding="utf-8")
    achado = re.search(r"google-adk(?:\[[^\]]*\])?==(\d+)\.(\d+)\.(\d+)", pyproject)
    p.checar("ADK com versão exata fixada (série 2, >= 2.2.0)", bool(achado) and (int(achado.group(1)), int(achado.group(2))) >= (2, 2), achado.group(0) if achado else "ausente")
    if achado:
        versao = ".".join(achado.groups())
        lock = (RAIZ / "uv.lock").read_text(encoding="utf-8")
        p.checar("uv.lock com a mesma versão", re.search(rf'name = "google-adk"\s+version = "{re.escape(versao)}"', lock) is not None, versao)

    base = next((r for r in ("upstream/main", "origin/main") if _git("rev-parse", "--verify", r).returncode == 0), None)
    if base:
        diff = _git("diff", "--quiet", base, "--", "dados")
        p.checar(f"dados/ idênticos aos de {base}", diff.returncode == 0)
    p.checar("dados/ sem alteração local", _git("status", "--porcelain", "--", "dados").stdout.strip() == "")

    p.checar(".env não versionado", _git("ls-files", ".env").stdout.strip() == "")
    chave = _git("grep", "-I", "-E", r"AIza[0-9A-Za-z_-]{35}", "--", ".", ":(exclude).github")
    p.checar("nenhuma chave do Google versionada", chave.returncode == 1, chave.stdout[:120])

    sys.path.insert(0, str(RAIZ / "src"))
    from aurora.agentes import instrucoes
    from aurora.agentes.principal import criar_agente_principal
    from aurora.runtime.testing import StubLlm

    raiz = criar_agente_principal(StubLlm())
    p.checar("principal com pelo menos dois especialistas", len(raiz.sub_agents) >= 2, [a.name for a in raiz.sub_agents])
    p.checar("principal sem tools", not raiz.tools)

    com_apartamento: list[str] = []
    for agente in raiz.sub_agents:
        for tool in agente.tools:
            try:
                props = (tool._get_declaration().parameters_json_schema or {}).get("properties", {})
            except Exception:
                import inspect

                props = inspect.signature(tool.func).parameters
            if "apartamento" in props:
                com_apartamento.append(tool.name)
    p.checar("nenhuma tool com parâmetro de apartamento", not com_apartamento, com_apartamento)

    regulamento = dados_regulamento().replace("*", "")
    amostras = [l.strip()[:60] for l in regulamento.splitlines() if len(l.strip()) >= 80]
    p.checar("INSTRUCAO_PRINCIPAL sem trecho do regulamento", not any(a in instrucoes.INSTRUCAO_PRINCIPAL for a in amostras))

    dominio = (RAIZ / "src" / "aurora" / "dados" / "dominio.py").read_text(encoding="utf-8")
    p.checar("exclusividade no banco (índice único + IntegrityError)", "CREATE UNIQUE INDEX" in dominio and "ux_reservas_ativa" in dominio and "IntegrityError" in dominio)

    readme = (RAIZ / "README.md").read_text(encoding="utf-8")
    p.checar("README com Arquitetura, Garantias e Como rodar", all(f"## {s}" in readme for s in ("Arquitetura", "Garantias", "Como rodar")))
    quebrados = []
    for caminho, linha in re.findall(r"\]\(([^)#\s]+)#L(\d+)\)", readme):
        arquivo = RAIZ / caminho
        if not arquivo.is_file() or int(linha) > len(arquivo.read_text(encoding="utf-8").splitlines()):
            quebrados.append(f"{caminho}#L{linha}")
    p.checar("links do README existem e a linha está no arquivo", not quebrados, quebrados)


def imprimir(relatorio: list[Passo], detalhado: bool) -> None:
    for p in relatorio:
        marca = "OK   " if p.ok else "FALHA"
        print(f"[{marca}] passo {p.numero:>2}: {p.titulo}")
        if p.erro:
            print(f"         erro: {p.erro}")
        for nome, ok, detalhe in p.checks:
            if detalhado or not ok:
                print(f"         {'ok ' if ok else 'ERR'} {nome}" + (f"  ({detalhe})" if detalhe and not ok else ""))
    falhas = [p.numero for p in relatorio if not p.ok]
    print(f"\n{len(relatorio) - len(falhas)}/{len(relatorio)} passos OK" + (f"; falharam: {falhas}" if falhas else ""))


def main() -> int:
    parser = argparse.ArgumentParser(prog="scripts/verificar_fluxo.py")
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--subir", action="store_true", help="restaura dados e sessões, sobe e reinicia a API sozinho")
    parser.add_argument("--subir-cmd", help="comando para subir a API (padrão: scripts/subir.py)")
    parser.add_argument("--timeout", type=float, default=180.0, help="segundos por requisição")
    parser.add_argument("-v", "--detalhado", action="store_true", help="lista também as verificações que passaram")
    args = parser.parse_args()

    api = Api(args.url, args.timeout)
    servidor = None
    if args.subir:
        comando = args.subir_cmd.split() if args.subir_cmd else [sys.executable, str(RAIZ / "scripts" / "subir.py")]
        restaurar = subprocess.run([sys.executable, str(RAIZ / "scripts" / "restaurar.py"), "--sessoes"], cwd=RAIZ)
        if restaurar.returncode != 0:
            print("restaurar.py falhou; abortando", file=sys.stderr)
            return 2
        servidor = Servidor(comando)
        servidor.subir(api)
    relatorio: list[Passo] = []
    try:
        executar(api, servidor, relatorio)
    finally:
        if servidor:
            servidor.parar()
    imprimir(relatorio, args.detalhado)
    return 0 if all(p.ok for p in relatorio) else 1


if __name__ == "__main__":
    sys.exit(main())
