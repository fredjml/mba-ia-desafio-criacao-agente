"""Roteiro de smoke E16 — escrito ANTES da implementação da tool.

VALIDAÇÃO (não ajustar se falhar; diagnosticar e reportar):
X1 estrutura: 14 capítulos e artigos segmentados; cada artigo tem capítulo;
   soma dos trechos ≈ o arquivo (nada perdido, sem duplicar).
X2 AC-21: horário da piscina aos domingos → Art. 22 primeiro, texto "9h às 20h";
   convidados na piscina → Art. 27; exame dermatológico → Art. 23;
   mais 6 pares pergunta→artigo (artigo esperado entre os 2 primeiros).
X3 AC-22: trechos só do capítulo esperado (piscina = só IV); soma ≤ 2500
   e < 10% do arquivo; "qual a capital da França?" → encontrado=false;
   nenhuma resposta contém âncora de capítulo não esperado.
X4 AC-23 / Garantia 4: nenhuma função pública devolve o texto completo;
   docstring e esquema de criar_tools_regulamento() sem texto do regulamento;
   esquema serializado < 1 KB.
X5 falha: AURORA_REGULAMENTO_PATH inexistente → indisponivel_temporariamente
   sem levantar; sem a variável, usa dados/regulamento.md.
X6 controle: caracteres devolvidos por pergunta versus ~44 KB do arquivo.
X7 generalização (conjunto de desenvolvimento): D1–D15 + paráfrases
   extras; o artigo esperado aparece nos trechos (ou encontrado=false /
   Cap. VI sem horário, conforme o caso). Escrito ANTES da mudança de ranking.
X8 validação cruzada: perguntas novas, metades A/B por semente fixa.
   Piso de não-regressão: A ≥ 14/19, B ≥ 8/17, negativos 10/10.
X9 desempenho: latência quente (mediana ≤ 100 ms, máximo ≤ 300 ms)
   nas perguntas do X7/X8 e concorrência curta (N=50, 0 divergências).

Sem modelo, sem chave, sem HTTP, sem embeddings, sem rede, sem ler .env.
"""

from __future__ import annotations

import inspect
import json
import os
import random
import re
import statistics
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from aurora.dados.carregar import diretorio_dados
from aurora.tools.regulamento import (
    VAR_REGULAMENTO,
    consultar_regulamento,
    criar_tools_regulamento,
)

REPO = Path(__file__).resolve().parents[3]
ARTIGO_RE = re.compile(r"^\*\*Art\.\s+(\d+)", re.MULTILINE)
CAPITULO_RE = re.compile(r"^## (Capítulo\s+.+)$", re.MULTILINE)

# Pares X2: (pergunta, artigo esperado, capítulo romano, deve ser o 1º?)
PERGUNTAS: list[tuple[str, str, str, bool]] = [
    ("Qual o horário da piscina aos domingos?", "Art. 22", "IV", True),
    ("Quantos convidados posso levar à piscina?", "Art. 27", "IV", False),
    ("Preciso de exame dermatológico para usar a piscina?", "Art. 23", "IV", False),
    (
        "Como são feitas as reservas do salão de festas, da churrasqueira e da quadra?",
        "Art. 36",
        "VI",
        False,
    ),
    (
        "Os cães precisam usar coleira refletiva cor de mostarda nas áreas comuns?",
        "Art. 54",
        "VIII",
        False,
    ),
    ("Qual o horário permitido para mudanças de móveis?", "Art. 62", "IX", False),
    ("Como devo separar o lixo para a coleta seletiva?", "Art. 84", "XII", False),
    ("Qual o período de silêncio no condomínio?", "Art. 16", "III", False),
    ("Qual a velocidade máxima permitida na garagem?", "Art. 78", "XI", False),
]

# X7 — conjunto de desenvolvimento (hold-out do orquestrador NÃO entra aqui).
# esperado: str | tuple[str, ...] | "false" | "d4"
#   str/tuple = artigo(s) aceitos entre os trechos; "1º" extra em primeiro
#   "false" = encontrado false; "d4" = false OU Cap. VI sem afirmar horário
DEV: list[tuple[str, str, str | tuple[str, ...] | None]] = [
    ("D1", "Até que horas posso fazer barulho à noite?", "Art. 16"),
    ("D2", "Posso ter um cachorro no apartamento?", "Art. 52"),
    ("D3", "Como faço para reservar a churrasqueira?", "Art. 36"),
    ("D4", "A quadra fecha que horas?", "d4"),
    ("D5", "Qual o horário da piscina aos domingos?", "1º:Art. 22"),
    ("D6", "Onde entrego o óleo de cozinha usado?", "Art. 85"),
    ("D7", "Quantos convidados posso levar à piscina?", "Art. 27"),
    ("D8", "Quem pode usar a academia?", "Art. 29"),
    ("D9", "Posso fazer obra no sábado?", "Art. 70"),
    ("D10", "Com quantos dias de antecedência agendo a mudança?", "Art. 61"),
    ("D11", "Qual a multa por infração?", "Art. 88"),
    ("D12", "Qual a velocidade máxima na garagem?", "Art. 78"),
    ("D13", "Cachorro pode ir no playground?", ("Art. 56", "Art. 35")),
    ("D14", "Qual é a capital da França?", "false"),
    ("D15", "Qual a receita de bolo de cenoura?", "false"),
    # Paráfrases extras (capítulos variados, vocabulário coloquial).
    ("E1", "Dá pra fumar no elevador?", "Art. 10"),
    ("E2", "Como autorizo uma visita?", "Art. 45"),
    ("E3", "Até quando a academia fica aberta?", "Art. 29"),
    ("E4", "Criança pode ficar na piscina sem acompanhante?", "Art. 24"),
    ("E5", "Onde guardo a bicicleta?", "Art. 82"),
    ("E6", "Encomenda fica quanto tempo na portaria?", "Art. 47"),
    ("E7", "Preciso cadastrar o animal na administração?", "Art. 53"),
    ("E8", "Pode lavar o carro na garagem?", "Art. 79"),
    ("E9", "O anfitrião precisa estar presente no salão durante o evento?", ("Art. 37", "Art. 36")),
    ("E10", "Como jogo fora um colchão velho?", "Art. 86"),
    ("E11", "A brinquedoteca fecha que horas?", "Art. 33"),
    ("E12", "Posso instalar carregador de carro elétrico?", "Art. 81"),
]

# Frases que só existem no capítulo indicado (vazamento = falha).
ANCORAS: dict[str, str] = {
    "I": "uso exclusivamente residencial",
    "III": "intervalo entre 22h e 8h",
    "IV": "9h às 20h",
    "VI": "reservas dessas áreas são feitas pelo aplicativo",
    "VIII": "coleira refletiva cor de mostarda",
    "IX": "plástico bolha lilás",
    "X": "lona xadrez vermelha",
    "XI": "dez quilômetros por hora",
    "XII": "coleta seletiva",
    "XIII": "advertência por escrito",
    "XIV": "revogando as disposições em contrário",
}


def _emit(event: str, **payload: Any) -> None:
    print(
        json.dumps({"event": event, **payload}, ensure_ascii=False, sort_keys=True),
        flush=True,
    )


def _arquivo() -> Path:
    return diretorio_dados() / "regulamento.md"


def _ler_arquivo() -> str:
    return _arquivo().read_text(encoding="utf-8")


def _segmentar_referencia(texto: str) -> list[dict[str, str]]:
    """Segmentação independente no smoke (não usa a tool)."""
    trechos: list[dict[str, str]] = []
    capitulo = ""
    linhas = texto.splitlines(keepends=True)
    atual: list[str] = []
    artigo = ""

    def fechar() -> None:
        nonlocal atual, artigo
        if artigo:
            trechos.append(
                {
                    "capitulo": capitulo,
                    "artigo": artigo,
                    "texto": "".join(atual),
                }
            )
        atual = []
        artigo = ""

    for linha in linhas:
        cap = CAPITULO_RE.match(linha.rstrip("\n"))
        art = ARTIGO_RE.match(linha)
        if cap:
            fechar()
            capitulo = cap.group(1).strip()
            continue
        if art:
            fechar()
            artigo = f"Art. {art.group(1)}"
            atual = [linha]
            continue
        if artigo:
            atual.append(linha)
    fechar()
    return trechos


def x1() -> list[dict[str, str]]:
    from aurora.tools import regulamento as mod

    bruto = _ler_arquivo()
    caps_arquivo = CAPITULO_RE.findall(bruto)
    arts_arquivo = ARTIGO_RE.findall(bruto)
    if len(caps_arquivo) != 14:
        raise AssertionError(f"capítulos no arquivo: {len(caps_arquivo)}")
    if len(arts_arquivo) != 96:
        raise AssertionError(f"artigos no arquivo: {len(arts_arquivo)}")

    internos = mod._carregar_trechos()
    if len(internos) != len(arts_arquivo):
        raise AssertionError(
            f"segmentados={len(internos)} artigos_arquivo={len(arts_arquivo)}"
        )
    if any(not item.get("capitulo") for item in internos):
        raise AssertionError("artigo sem capítulo")

    ref = _segmentar_referencia(bruto)
    if len(ref) != len(internos):
        raise AssertionError(f"ref={len(ref)} internos={len(internos)}")

    arts_int = [item["artigo"] for item in internos]
    if len(arts_int) != len(set(arts_int)):
        raise AssertionError(f"artigos duplicados: {arts_int}")
    if arts_int != [item["artigo"] for item in ref]:
        raise AssertionError("ordem/identidade dos artigos diverge da referência")

    soma_int = sum(len(item["texto"]) for item in internos)
    soma_ref = sum(len(item["texto"]) for item in ref)
    if soma_int != soma_ref:
        raise AssertionError(f"soma interna {soma_int} != ref {soma_ref}")
    if abs(soma_int - len(bruto)) > len(bruto) * 0.15:
        raise AssertionError(
            f"soma {soma_int} distante do arquivo {len(bruto)}"
        )

    _emit(
        "x1_ok",
        capitulos=len(caps_arquivo),
        artigos=len(internos),
        soma_trechos=soma_int,
        tamanho_arquivo=len(bruto),
    )
    return internos


def _artigos(resposta: dict[str, Any]) -> list[str]:
    return [str(item.get("artigo", "")) for item in resposta.get("trechos") or []]


def _texto_junto(resposta: dict[str, Any]) -> str:
    return " ".join(str(item.get("texto", "")) for item in resposta.get("trechos") or [])


def x2() -> list[tuple[str, dict[str, Any]]]:
    resultados: list[tuple[str, dict[str, Any]]] = []
    pares: list[dict[str, Any]] = []
    for pergunta, esperado, _cap, primeiro in PERGUNTAS:
        resp = consultar_regulamento(pergunta)
        resultados.append((pergunta, resp))
        if resp.get("encontrado") is not True:
            raise AssertionError(f"{pergunta!r} nao encontrado: {resp}")
        artigos = _artigos(resp)
        if primeiro:
            if not artigos or artigos[0] != esperado:
                raise AssertionError(
                    f"{pergunta!r}: esperado {esperado} em 1º, veio {artigos}"
                )
        elif esperado not in artigos[:2]:
            raise AssertionError(
                f"{pergunta!r}: esperado {esperado} entre os 2 primeiros, veio {artigos}"
            )
        if pergunta.startswith("Qual o horário da piscina") and "9h às 20h" not in _texto_junto(
            resp
        ):
            raise AssertionError(f"Art. 22 sem '9h às 20h': {resp}")
        pares.append(
            {
                "pergunta": pergunta,
                "esperado": esperado,
                "artigos": artigos,
                "primeiro_obrigatorio": primeiro,
            }
        )
    _emit("x2_ok", pares=pares)
    return resultados


def x3(resultados: list[tuple[str, dict[str, Any]]]) -> None:
    bruto = _ler_arquivo()
    tamanho = len(bruto)
    limite = 2500
    dez_por_cento = tamanho * 0.10

    for pergunta, cap, *_ in ((p[0], p[2]) for p in PERGUNTAS):
        resp = next(item for item in resultados if item[0] == pergunta)[1]
        soma = sum(len(str(t.get("texto", ""))) for t in resp.get("trechos") or [])
        if soma > limite:
            raise AssertionError(f"{pergunta!r}: {soma} > {limite}")
        if soma >= dez_por_cento:
            raise AssertionError(f"{pergunta!r}: {soma} >= 10% ({dez_por_cento})")
        for trecho in resp.get("trechos") or []:
            capitulo = str(trecho.get("capitulo", ""))
            if f"Capítulo {cap}" not in capitulo and f"Capitulo {cap}" not in capitulo:
                raise AssertionError(
                    f"{pergunta!r}: trecho fora do capítulo {cap}: {capitulo}"
                )
        blob = _texto_junto(resp) + " " + json.dumps(resp, ensure_ascii=False)
        for outro, ancora in ANCORAS.items():
            if outro == cap:
                continue
            if ancora in blob:
                raise AssertionError(
                    f"{pergunta!r}: vazou âncora do capítulo {outro}: {ancora!r}"
                )

    irrelevante = consultar_regulamento("qual a capital da França?")
    if irrelevante.get("encontrado") is not False or irrelevante.get("trechos") != []:
        raise AssertionError(f"irrelevante deveria ser vazio: {irrelevante}")
    if irrelevante.get("status") == "indisponivel_temporariamente":
        raise AssertionError("irrelevante não é falha de infra")
    _emit("x3_ok", irrelevante=irrelevante, tamanho_arquivo=tamanho)


def x4() -> None:
    from aurora.tools import regulamento as mod

    bruto = _ler_arquivo()
    publicas = [
        (nome, obj)
        for nome, obj in inspect.getmembers(mod)
        if inspect.isfunction(obj) and not nome.startswith("_")
    ]
    for nome, func in publicas:
        if nome == "consultar_regulamento":
            resp = func("Qual o horário da piscina aos domingos?")
            serial = json.dumps(resp, ensure_ascii=False)
            if len(serial) >= len(bruto) * 0.5:
                raise AssertionError(f"{nome} devolveu texto demais")
            if bruto in serial:
                raise AssertionError(f"{nome} devolveu o arquivo inteiro")
        elif nome == "criar_tools_regulamento":
            tools = func()
            if not tools:
                raise AssertionError("criar_tools_regulamento vazio")
        elif nome == "caminho_regulamento":
            caminho = func()
            if not isinstance(caminho, Path):
                raise AssertionError(f"caminho_regulamento não é Path: {caminho!r}")
        else:
            raise AssertionError(f"função pública inesperada: {nome}")

    tools = criar_tools_regulamento()
    if len(tools) != 1:
        raise AssertionError(tools)
    tool = tools[0]
    schema = tool._get_declaration().parameters_json_schema
    serial_schema = json.dumps(schema, ensure_ascii=False)
    if len(serial_schema.encode("utf-8")) >= 1024:
        raise AssertionError(f"schema grande: {len(serial_schema)} {schema}")
    props = schema.get("properties") or {}
    if list(props) != ["pergunta"]:
        raise AssertionError(f"schema visível deve ser só pergunta: {schema}")
    if "apartamento" in serial_schema or "tool_context" in serial_schema:
        raise AssertionError(schema)

    ancora_doc = "plástico bolha lilás"
    docs = [
        mod.__doc__ or "",
        consultar_regulamento.__doc__ or "",
        criar_tools_regulamento.__doc__ or "",
        tool.description or "",
        serial_schema,
    ]
    for bloco in docs:
        if ancora_doc in bloco or "9h às 20h" in bloco or bruto[:200] in bloco:
            raise AssertionError("docstring/esquema contém texto do regulamento")

    _emit(
        "x4_ok",
        publicas=[nome for nome, _ in publicas],
        schema=schema,
        schema_bytes=len(serial_schema.encode("utf-8")),
        confirmacao=bool(getattr(tool, "_require_confirmation", False)),
    )


def x5() -> None:
    anterior = os.environ.get(VAR_REGULAMENTO)
    os.environ[VAR_REGULAMENTO] = r"Z:\caminho\que\nao\existe\regulamento.md"
    try:
        resp = consultar_regulamento("Qual o horário da piscina aos domingos?")
        if resp != {"status": "indisponivel_temporariamente"}:
            raise AssertionError(f"path inexistente: {resp}")
    finally:
        if anterior is None:
            os.environ.pop(VAR_REGULAMENTO, None)
        else:
            os.environ[VAR_REGULAMENTO] = anterior

    if VAR_REGULAMENTO in os.environ:
        os.environ.pop(VAR_REGULAMENTO, None)
    ok = consultar_regulamento("Qual o horário da piscina aos domingos?")
    if ok.get("encontrado") is not True:
        raise AssertionError(f"sem variável deveria achar o arquivo: {ok}")
    _emit("x5_ok", sem_variavel_encontrado=True, var=VAR_REGULAMENTO)


def x6(resultados: list[tuple[str, dict[str, Any]]]) -> None:
    tamanho = len(_ler_arquivo())
    linhas = []
    for pergunta, resp in resultados:
        soma = sum(len(str(t.get("texto", ""))) for t in resp.get("trechos") or [])
        linhas.append(
            {
                "pergunta": pergunta,
                "chars_retorno": soma,
                "chars_arquivo": tamanho,
                "razao": round(soma / tamanho, 4) if tamanho else None,
            }
        )
    irrelevante = consultar_regulamento("qual a capital da França?")
    linhas.append(
        {
            "pergunta": "qual a capital da França?",
            "chars_retorno": sum(
                len(str(t.get("texto", ""))) for t in irrelevante.get("trechos") or []
            ),
            "chars_arquivo": tamanho,
            "razao": 0,
        }
    )
    _emit("x6_controle", arquivo_chars=tamanho, por_pergunta=linhas)


def _d4_ok(resp: dict[str, Any]) -> bool:
    if resp.get("encontrado") is False and resp.get("trechos") == []:
        return True
    if resp.get("encontrado") is not True:
        return False
    for trecho in resp.get("trechos") or []:
        capitulo = str(trecho.get("capitulo", ""))
        if "Capítulo VI" not in capitulo and "Capitulo VI" not in capitulo:
            return False
        if re.search(r"\d+\s*h", str(trecho.get("texto", "")), flags=re.IGNORECASE):
            return False
    return True


def _dev_ok(esperado: str | tuple[str, ...], resp: dict[str, Any]) -> bool:
    if esperado == "false":
        return resp.get("encontrado") is False and resp.get("trechos") == []
    if esperado == "d4":
        return _d4_ok(resp)
    if resp.get("encontrado") is not True:
        return False
    artigos = _artigos(resp)
    if isinstance(esperado, str) and esperado.startswith("1º:"):
        alvo = esperado.split(":", 1)[1]
        return bool(artigos) and artigos[0] == alvo
    aceitos = esperado if isinstance(esperado, tuple) else (esperado,)
    return any(item in artigos for item in aceitos)


# X8 — perguntas novas (não são D/E). esperado: artigo | tuple | "false"
# metade: preenchida por semente; limiar escolhido olhando só a metade A.
X8: list[tuple[str, str, str | tuple[str, ...]]] = [
    ("P01", "O prédio pode virar loja? Meu cunhado quer atender cliente aqui.", "Art. 2"),
    ("P02", "Quem ajuda o síndico a cuidar das contas do prédio?", "Art. 3"),
    ("P03", "Meu filho de 8 anos sobe de elevador sozinho, pode?", "Art. 14"),
    ("P04", "Atrasei o boleto uns 20 dias, o que acontece comigo?", "Art. 11"),
    ("P05", "O vizinho liga o som alto de madrugada, até que hora isso pode?", "Art. 16"),
    ("P06", "Posso pendurar um sino de vento na sacada?", "Art. 20"),
    ("P07", "Domingo de manhã já dá para entrar na piscina?", "Art. 22"),
    ("P08", "Levo 6 amigos para nadar, cabe todo mundo?", "Art. 27"),
    ("P09", "Meu sobrinho de 15 anos quer malhar sozinho, deixam?", "Art. 29"),
    ("P10", "A salinha de brincar das crianças fecha cedo?", "Art. 33"),
    ("P11", "Quero um churrasco sábado com uns 15 amigos, como peço o espaço?", "Art. 36"),
    ("P12", "Se a galera quebrar cadeira na festa, quem paga o conserto?", "Art. 43"),
    ("P13", "O motoboy pode subir com a pizza até a minha porta?", "Art. 47"),
    ("P14", "Perdi o controle do portão, e agora?", "Art. 48"),
    ("P15", "Gato pode morar comigo no ap?", "Art. 52"),
    ("P16", "Meu cachorro fez sujeira no corredor, tenho que limpar na hora?", "Art. 57"),
    ("P17", "Vou me mudar dia 15, aviso com quanto tempo antes?", "Art. 61"),
    ("P18", "Dá para levar o sofá pelo elevador social?", "Art. 63"),
    ("P19", "Quero quebrar uma parede no sábado de manhã, pode?", "Art. 70"),
    ("P20", "O entulho pode ficar no corredor a semana toda?", "Art. 72"),
    ("P21", "Posso deixar um armário velho na minha vaga?", "Art. 76"),
    ("P22", "Meu carro elétrico, posso pôr tomada na vaga?", "Art. 81"),
    ("P23", "Onde jogo pilha e lâmpada queimada?", "Art. 85"),
    ("P24", "Celular velho vai no lixo normal?", "Art. 87"),
    ("P25", "Tomei uma multa, quantos dias tenho para me defender?", "Art. 90"),
    ("P26", "Dá para mudar essa regra numa reunião dos moradores?", "Art. 93"),
    ("P27", "Ninguém me entregou cópia das regras, preciso cumprir mesmo assim?", "Art. 94"),
    ("P28", "Preciso de atestado de pele para usar a piscina?", "Art. 23"),
    ("P29", "O parquinho é só para criança pequena?", "Art. 34"),
    ("P30", "Cachorro pode entrar no parquinho?", "Art. 56"),
    ("P31", "Andar depressa de carro lá embaixo na garagem, qual o limite?", "Art. 78"),
    ("P32", "Festa dentro de casa de madrugada incomoda os vizinhos?", "Art. 17"),
    ("P33", "As câmeras filmam o hall o tempo todo?", "Art. 49"),
    ("P34", "Mudança no domingo de manhã, deixam?", "Art. 62"),
    ("P35", "Quem reserva o salão tem que ficar na festa inteira?", "Art. 37"),
    ("P36", "Quero alugar meu ap, aviso alguém da administração?", "Art. 5"),
    ("N01", "Qual a capital da França?", "false"),
    ("N02", "Qual a receita de bolo de cenoura?", "false"),
    ("N03", "Qual o telefone da pizzaria da esquina?", "false"),
    ("N04", "Meu filho tirou 15 na prova de matemática, e agora?", "false"),
    ("N05", "Como faço um bolo de cenoura para 15 pessoas?", "false"),
    ("N06", "Qual o preço do bitcoin hoje?", "false"),
    ("N07", "Qual o melhor time de futebol do Brasil?", "false"),
    ("N08", "Me indica um filme para ver à noite?", "false"),
    ("N09", "A capital da França fica na Europa?", "false"),
    ("N10", "Quantos habitantes tem a minha cidade?", "false"),
]


def _metades_x8() -> tuple[set[str], set[str]]:
    ids = [item[0] for item in X8]
    rng = random.Random(20260925)
    rng.shuffle(ids)
    corte = len(ids) // 2
    return set(ids[:corte]), set(ids[corte:])


def x8() -> None:
    metade_a, metade_b = _metades_x8()
    tabela_b: list[dict[str, Any]] = []
    pos = {"A": [0, 0], "B": [0, 0]}
    neg_falhas: list[str] = []
    for codigo, pergunta, esperado in X8:
        resp = consultar_regulamento(pergunta)
        artigos = _artigos(resp)
        ok = _dev_ok(esperado, resp)
        metade = "A" if codigo in metade_a else "B"
        if esperado == "false":
            if not ok:
                neg_falhas.append(f"{codigo}:{artigos}")
        else:
            pos[metade][1] += 1
            if ok:
                pos[metade][0] += 1
        if metade == "B" or esperado == "false":
            tabela_b.append(
                {
                    "id": codigo,
                    "metade": metade,
                    "pergunta": pergunta,
                    "esperado": list(esperado) if isinstance(esperado, tuple) else esperado,
                    "artigos": artigos,
                    "ok": ok,
                }
            )
    taxa = {
        lado: round(acerto / total, 3) if total else None
        for lado, (acerto, total) in pos.items()
    }
    _emit(
        "x8_cruzada",
        taxa_positivos=taxa,
        positivos=pos,
        negativos_falhas=neg_falhas,
        metade_b_e_negativos=tabela_b,
    )
    if neg_falhas:
        raise AssertionError("X8 negativos: " + "; ".join(neg_falhas))
    acerto_a, total_a = pos["A"]
    acerto_b, total_b = pos["B"]
    # piso de não-regressão; a meta original (B ≥ 80%) não é atingível por busca lexical sem modelo; aprovado pelo owner em 2026-09-25
    if acerto_a < 14 or total_a != 19:
        raise AssertionError(f"X8 metade A {acerto_a}/{total_a}")
    if acerto_b < 8 or total_b != 17:
        raise AssertionError(f"X8 metade B {acerto_b}/{total_b}")


def x7() -> None:
    tabela: list[dict[str, Any]] = []
    falhas: list[str] = []
    for codigo, pergunta, esperado in DEV:
        resp = consultar_regulamento(pergunta)
        artigos = _artigos(resp)
        ok = _dev_ok(esperado, resp)
        tabela.append(
            {
                "id": codigo,
                "pergunta": pergunta,
                "esperado": list(esperado) if isinstance(esperado, tuple) else esperado,
                "encontrado": resp.get("encontrado"),
                "artigos": artigos,
                "ok": ok,
            }
        )
        if not ok:
            falhas.append(f"{codigo} artigos={artigos} resp={resp.get('encontrado')}")
    _emit("x7_dev", casos=tabela, falhas=falhas)
    if falhas:
        raise AssertionError("X7 falhou: " + "; ".join(falhas))


def x9() -> None:
    perguntas = [pergunta for _, pergunta, _ in DEV] + [pergunta for _, pergunta, _ in X8]
    for pergunta in perguntas:
        consultar_regulamento(pergunta)
    tempos: list[float] = []
    for pergunta in perguntas:
        t0 = time.perf_counter()
        consultar_regulamento(pergunta)
        tempos.append((time.perf_counter() - t0) * 1000.0)
    mediana = statistics.median(tempos)
    maximo = max(tempos)

    amostra = perguntas[:50]
    sequencial = [consultar_regulamento(pergunta) for pergunta in amostra]
    concorrente: list[dict[str, Any] | None] = [None] * len(amostra)

    def worker(i: int, pergunta: str) -> None:
        concorrente[i] = consultar_regulamento(pergunta)

    threads = [
        threading.Thread(target=worker, args=(i, pergunta))
        for i, pergunta in enumerate(amostra)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    divergencias = sum(1 for a, b in zip(sequencial, concorrente) if a != b)
    _emit(
        "x9_desempenho",
        n=len(tempos),
        mediana_ms=round(mediana, 3),
        maximo_ms=round(maximo, 3),
        concorrencia_n=len(amostra),
        divergencias=divergencias,
    )
    if mediana > 100 or maximo > 300:
        raise AssertionError(f"X9 latência mediana={mediana:.1f} ms máximo={maximo:.1f} ms")
    if divergencias:
        raise AssertionError(f"X9 concorrência: {divergencias} divergências")


def main() -> int:
    inicio = time.perf_counter()
    try:
        x1()
        resultados = x2()
        x3(resultados)
        x4()
        x5()
        x6(resultados)
        x7()
        x8()
        x9()
        _emit(
            "smoke_end",
            status="PASS",
            segundos=round(time.perf_counter() - inicio, 3),
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        _emit(
            "smoke_end",
            status="FAIL",
            erro=f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc(),
            segundos=round(time.perf_counter() - inicio, 3),
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
