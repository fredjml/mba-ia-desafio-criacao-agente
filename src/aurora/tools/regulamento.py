"""Consulta pontual ao regulamento interno (trechos, nunca o arquivo inteiro).

Carregamento lazy e cacheado no processo. Busca determinística por
palavras-chave (stdlib). O texto completo não é exposto por API pública.
"""

from __future__ import annotations

import logging
import math
import os
import re
import threading
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

from google.adk.tools import FunctionTool

from aurora.dados.carregar import diretorio_dados as _diretorio_dados
from aurora.tools.reservas import STATUS_INDISPONIVEL

logger = logging.getLogger("aurora.tools")

VAR_REGULAMENTO = "AURORA_REGULAMENTO_PATH"

_ARTIGO_RE = re.compile(r"^\*\*Art\.\s+(\d+)", re.MULTILINE)
_CAPITULO_RE = re.compile(r"^## (Capítulo\s+.+)$", re.MULTILINE)
_TOKEN_RE = re.compile(r"[a-z0-9]+")

_MAX_TRECHOS = 3
_MAX_CHARS = 2500
_K1 = 1.5
_B = 0.75
_BONUS_TITULO = 1.2
# Sobreposição do cluster de tempo (pergunta e artigo), não de relógio.
_BONUS_TEMPO = 1.2
# "Sem resposta" é limiar absoluto sobre o que CASOU no melhor artigo.
# Um termo só, por mais raro, não basta (palavra solta tipo "capital").
# A soma de IDF dos termos casados tem de passar de um piso fixo:
# dois termos muito comuns não chegam; dois termos de conteúdo sim.
_MIN_TERMOS_CASADOS = 2
_IDF_MIN = 2.0
# Resto de um prefixo comum só conta se for flexão, não outra palavra.
_SUFIXO_FLEX = frozenset(
    {"s", "es", "a", "o", "as", "os", "ada", "ado", "adas", "ados", "ar", "er", "ir"}
)

_STOP = frozenset(
    {
        "a",
        "o",
        "os",
        "as",
        "um",
        "uma",
        "uns",
        "umas",
        "de",
        "da",
        "do",
        "das",
        "dos",
        "em",
        "no",
        "na",
        "nos",
        "nas",
        "por",
        "para",
        "pelo",
        "pela",
        "pelos",
        "pelas",
        "com",
        "sem",
        "e",
        "ou",
        "que",
        "se",
        "ao",
        "aos",
        "qual",
        "quais",
        "como",
        "quando",
        "onde",
        "quanto",
        "quantos",
        "quantas",
        "posso",
        "pode",
        "podem",
        "preciso",
        "precisa",
        "deve",
        "devem",
        "meu",
        "minha",
        "meus",
        "minhas",
        "seu",
        "sua",
        "seus",
        "suas",
        "este",
        "esta",
        "isso",
        "aquele",
        "sobre",
        "mais",
        "menos",
        "muito",
        "pouco",
        "ter",
        "tem",
        "faz",
        "fazer",
        "ser",
        "esta",
        "algum",
        "alguma",
        "qualquer",
        "cada",
        "todo",
        "toda",
        "todos",
        "todas",
        "eu",
        "voce",
        "me",
        "te",
        "ate",
        "pra",
        "pro",
        "quem",
        "fica",
        "ficar",
        "ficam",
        "faco",
        "faz",
        "fiz",
        "feito",
        "tenho",
        "vou",
        "vai",
        "vamos",
        "ir",
        "foi",
        "ha",
        "apos",
        "entre",
        "bem",
        "ja",
        "ainda",
        "tambem",
        "aqui",
        "ali",
        "la",
        "so",
        "quanto",
        "fora",
        "dum",
        "duma",
        "dele",
        "dela",
        "agora",
        "nao",
        "entao",
        "porque",
        "pois",
        "ele",
        "ela",
        "eles",
        "elas",
        "voces",
        "depois",
        "antes",
        "sempre",
        "nunca",
        "tinha",
        "tinham",
        "estou",
        "estamos",
        "estao",
        "sou",
        "sao",
        "somos",
        "poder",
        "dever",
        "estar",
        "ai",
        "ne",
        "tipo",
        "assim",
    }
)

# Termos de domínio que aparecem em quase qualquer artigo: entram no
# BM25 para desempate, mas sozinhos não autorizam resposta.
_GENERICO = frozenset(
    {
        "condominio",
        "apartamento",
        "morador",
        "regulamento",
        "unidade",
        "edificio",
        "interno",
        "residencial",
        "aurora",
    }
)

# Cluster geral de tempo/funcionamento (substitui o bônus por \d+h).
# Um termo da pergunta neste conjunto casa se o artigo tiver qualquer
# outro termo do conjunto — sem olhar relógio no texto.
_TEMPO = frozenset(
    {
        "hora",
        "horario",
        "periodo",
        "funciona",
        "funcionamento",
        "abre",
        "aberto",
        "fecha",
        "fechado",
        "expediente",
        "noite",
        "madrugada",
        "manha",
        "tarde",
        "disponivel",
    }
)

# Sinônimos de língua geral (pt-BR). Classes de vocabulário, não pergunta→artigo.
# No máximo 40 entradas.
_EXPANSAO: dict[str, tuple[str, ...]] = {
    # animais domésticos, qualquer espécie comum
    "cachorro": ("cao", "animal"),
    "cao": ("animal",),
    "gato": ("animal",),
    "pet": ("animal",),
    "bicho": ("animal",),
    # barulho cotidiano vs. ruído/som
    "barulho": ("ruido", "som"),
    "barulhento": ("ruido", "som"),
    # criança na fala comum vs. menor
    "crianca": ("menor",),
    # veículo na fala comum
    "carro": ("veiculo",),
    "moto": ("veiculo", "motocicleta"),
    # lixo cotidiano vs. resíduo
    "lixo": ("residuo",),
    # reforma cotidiana vs. obra
    "reforma": ("obra",),
    # visita informal vs. visitante
    "visita": ("visitante", "convidado"),
    # encomenda / entrega
    "encomenda": ("entrega", "mercadoria"),
    "pacote": ("entrega", "encomenda"),
    # quem leva a encomenda
    "motoboy": ("entrega",),
    "entregador": ("entrega",),
    # dinheiro / pagamento
    "boleto": ("pagamento", "cota"),
    "dinheiro": ("pagamento",),
    "pagar": ("pagamento",),
    # venda / comércio
    "vender": ("comercial", "comercio"),
    "loja": ("comercial",),
    # quebrar / estragar / dano
    "quebrar": ("dano", "avaria"),
    "estragar": ("dano",),
    # comer / alimentar
    "comer": ("alimento",),
    "pizza": ("refeicao",),
    # agendar vs. substantivo reserva
    "reservar": ("reserva",),
    # churrasco (evento) vs. churrasqueira (equipamento)
    "churrasco": ("churrasqueira",),
    # nadar / malhar: atividade vs. espaço genérico da língua
    "nadar": ("piscina",),
    "malhar": ("academia", "exercicio"),
    # moradia na fala comum vs. unidade
    "apartamento": ("unidade",),
    "ap": ("unidade",),
    # sem companhia vs. desacompanhado
    "sozinho": ("desacompanhado",),
    # móveis na fala comum
    "sofa": ("movel",),
    "armario": ("movel",),
    "geladeira": ("movel",),
    # área de brincar vs. playground
    "parquinho": ("playground",),
    # jogar fora vs. descarte
    "jogar": ("descarte",),
    # pressa vs. velocidade
    "depressa": ("velocidade",),
    # festa vs. evento
    "festa": ("evento",),
}

_cache_lock = threading.Lock()
_cache: tuple[str, "_Indice"] | None = None


class _ArtigoIdx:
    __slots__ = ("trecho", "tf", "titulo", "vocab", "dl")

    def __init__(
        self,
        trecho: dict[str, str],
        tf: Counter[str],
        titulo: set[str],
        vocab: set[str],
        dl: int,
    ) -> None:
        self.trecho = trecho
        self.tf = tf
        self.titulo = titulo
        self.vocab = vocab
        self.dl = dl


class _Indice:
    """Índice imutável do arquivo, mais memo por termo (int/frozenset)."""

    __slots__ = (
        "trechos",
        "artigos",
        "df",
        "n_docs",
        "avgdl",
        "vocabulario",
        "por_prefixo",
        "memo_prefixo",
        "memo_df",
        "memo_lock",
    )

    def __init__(
        self,
        trechos: list[dict[str, str]],
        artigos: list[_ArtigoIdx],
        df: Counter[str],
        n_docs: int,
        avgdl: float,
        vocabulario: frozenset[str],
        por_prefixo: dict[str, tuple[str, ...]],
    ) -> None:
        self.trechos = trechos
        self.artigos = artigos
        self.df = df
        self.n_docs = n_docs
        self.avgdl = avgdl
        self.vocabulario = vocabulario
        self.por_prefixo = por_prefixo
        self.memo_prefixo: dict[str, frozenset[str]] = {}
        self.memo_df: dict[str, int] = {}
        self.memo_lock = threading.Lock()


def caminho_regulamento() -> Path:
    env = os.environ.get(VAR_REGULAMENTO)
    if env:
        return Path(env)
    return _diretorio_dados() / "regulamento.md"


def _sem_acento(texto: str) -> str:
    decomp = unicodedata.normalize("NFD", texto)
    return "".join(c for c in decomp if unicodedata.category(c) != "Mn").lower()


def _radical(token: str) -> str:
    if len(token) > 4 and token.endswith("oes"):
        return token[:-3] + "ao"
    if len(token) > 4 and token.endswith("aes"):
        return token[:-3] + "ao"
    if len(token) > 4 and token.endswith("eis"):
        return token[:-3] + "el"
    if len(token) > 4 and token.endswith("ais"):
        return token[:-3] + "al"
    if len(token) > 4 and token.endswith("res"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _genero(token: str) -> str:
    # Troca a/o só em palavras longas: "bolo"/"bola" não são o mesmo termo.
    if len(token) >= 6 and token.endswith("a"):
        return token[:-1] + "o"
    if len(token) >= 6 and token.endswith("o"):
        return token[:-1] + "a"
    return token


def _formas_flexao(token: str) -> set[str]:
    """Flexões comuns do português: 1ª pessoa, infinitivo, particípio."""
    extra: set[str] = set()
    if len(token) >= 4 and token.endswith("o"):
        raiz = token[:-1]
        extra.update(
            {
                raiz,
                raiz + "ar",
                raiz + "ada",
                raiz + "ado",
                raiz + "adas",
                raiz + "ados",
                raiz + "acao",
            }
        )
        # pagar/pague, entregar/entregue: g do radical pede "ue"
        if raiz.endswith("g"):
            extra.add(raiz + "ue")
    if len(token) >= 4 and token.endswith("ar"):
        raiz = token[:-2]
        extra.update({raiz, token[:-1], raiz + "o", raiz + "a", raiz + "ado", raiz + "ada"})
    if len(token) >= 5 and token.endswith("ei"):
        raiz = token[:-2]
        extra.update({raiz + "ar", raiz + "o", raiz + "a", raiz + "ado"})
    return extra


def _prefixo_compativel(a: str, b: str) -> bool:
    menor, maior = (a, b) if len(a) <= len(b) else (b, a)
    if not menor or len(menor) < 4 or not maior.startswith(menor):
        return False
    return maior[len(menor) :] in _SUFIXO_FLEX


def _expansoes(term: str) -> tuple[str, ...]:
    vistos: list[str] = []
    chaves = [term, _genero(term), *_formas_flexao(term)]
    for chave in chaves:
        for extra in _EXPANSAO.get(chave, ()):
            radical = _radical(_sem_acento(extra))
            if radical not in vistos:
                vistos.append(radical)
    return tuple(vistos)


def _tokens(texto: str) -> list[str]:
    brutos = _TOKEN_RE.findall(_sem_acento(texto))
    saida: list[str] = []
    for token in brutos:
        if token in _STOP or len(token) < 2 or token.isdigit():
            continue
        saida.append(_radical(token))
    return saida


def _calcular_casamentos(indice: _Indice, term: str) -> frozenset[str]:
    """Tokens do vocabulário com o mesmo casamento de `_prefixo_compativel`."""
    if len(term) < 4:
        return frozenset()
    achados: set[str] = set()
    for token in indice.por_prefixo.get(term, ()):
        if token[len(term) :] in _SUFIXO_FLEX:
            achados.add(token)
    vocabulario = indice.vocabulario
    for sufixo in _SUFIXO_FLEX:
        if len(term) - len(sufixo) >= 4 and term.endswith(sufixo):
            radical = term[: -len(sufixo)]
            if radical in vocabulario:
                achados.add(radical)
    return frozenset(achados)


def _casamentos(indice: _Indice, term: str) -> frozenset[str]:
    achado = indice.memo_prefixo.get(term)
    if achado is not None:
        return achado
    calculado = _calcular_casamentos(indice, term)
    with indice.memo_lock:
        return indice.memo_prefixo.setdefault(term, calculado)


def _esta_em(term: str, vocab: set[str], indice: _Indice) -> bool:
    candidatos = {term, _genero(term), *_formas_flexao(term)}
    if candidatos & vocab:
        return True
    for forma in candidatos:
        if _casamentos(indice, forma) & vocab:
            return True
    return False


def _termo_coberto(term: str, vocab: set[str], indice: _Indice) -> bool:
    if _esta_em(term, vocab, indice):
        return True
    for extra in _expansoes(term):
        if _esta_em(extra, vocab, indice):
            return True
    return False


def _termos_query(pergunta: str) -> tuple[list[str], list[str], list[str]]:
    originais: list[str] = []
    for token in _tokens(pergunta):
        if token not in originais:
            originais.append(token)
    conteudo = list(originais)
    ampliados = list(originais)
    for token in originais:
        for extra in _expansoes(token):
            if extra not in ampliados and extra not in _STOP:
                ampliados.append(extra)
    return originais, conteudo, ampliados


def _segmentar(texto: str) -> list[dict[str, str]]:
    trechos: list[dict[str, str]] = []
    capitulo = ""
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

    for linha in texto.splitlines(keepends=True):
        cap = _CAPITULO_RE.match(linha.rstrip("\n"))
        art = _ARTIGO_RE.match(linha)
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


def _por_prefixo_de(vocabulario: frozenset[str]) -> dict[str, tuple[str, ...]]:
    por: dict[str, list[str]] = {}
    for token in vocabulario:
        if len(token) < 4:
            continue
        for i in range(4, len(token) + 1):
            por.setdefault(token[:i], []).append(token)
    return {chave: tuple(tokens) for chave, tokens in por.items()}


def _construir_indice(trechos: list[dict[str, str]]) -> _Indice:
    artigos: list[_ArtigoIdx] = []
    vocabulario: set[str] = set()
    df: Counter[str] = Counter()
    soma_dl = 0
    for trecho in trechos:
        toks = _tokens(trecho["texto"])
        tf = Counter(toks)
        titulo = _titulo_tokens(trecho["capitulo"])
        dl = len(toks)
        artigos.append(_ArtigoIdx(trecho, tf, titulo, set(tf) | titulo, dl))
        df.update(tf.keys())
        soma_dl += dl
        vocabulario.update(tf.keys())
        vocabulario.update(titulo)
    n_docs = len(artigos)
    vocab_fx = frozenset(vocabulario)
    return _Indice(
        trechos,
        artigos,
        df,
        n_docs,
        soma_dl / max(n_docs, 1),
        vocab_fx,
        _por_prefixo_de(vocab_fx),
    )


def _obter_indice() -> _Indice:
    path = caminho_regulamento()
    chave = str(path)
    global _cache
    with _cache_lock:
        if _cache is not None and _cache[0] == chave:
            return _cache[1]
        conteudo = path.read_text(encoding="utf-8")
        indice = _construir_indice(_segmentar(conteudo))
        _cache = (chave, indice)
        return indice


def _carregar_trechos() -> list[dict[str, str]]:
    return _obter_indice().trechos


def _titulo_tokens(capitulo: str) -> set[str]:
    parte = capitulo.split(":", 1)[-1]
    return set(_tokens(parte))


def _idf_df(df_count: int, n_docs: int) -> float:
    return math.log((n_docs - df_count + 0.5) / (df_count + 0.5) + 1.0)


def _idf(term: str, df: Counter[str], n_docs: int) -> float:
    return _idf_df(df.get(term, 0), n_docs)


def _df_casado(indice: _Indice, term: str) -> int:
    """Maior DF entre o termo, flexões e expansões que existem no corpus."""
    achado = indice.memo_df.get(term)
    if achado is not None:
        return achado
    candidatos = {term, *_formas_flexao(term), *_expansoes(term)}
    if term in _TEMPO:
        candidatos.update(_TEMPO)
    melhor = 0
    df = indice.df
    for cand in candidatos:
        melhor = max(melhor, df.get(cand, 0))
        for chave in _casamentos(indice, cand):
            melhor = max(melhor, df.get(chave, 0))
    with indice.memo_lock:
        return indice.memo_df.setdefault(term, melhor)


def _freq_casada(indice: _Indice, term: str, tf: Counter[str]) -> int:
    if term in tf:
        return tf[term]
    return sum(tf[tok] for tok in _casamentos(indice, term) if tok in tf)


def _score_bm25(
    termos: list[str],
    tf: Counter[str],
    titulo: set[str],
    dl: int,
    avgdl: float,
    n_docs: int,
    indice: _Indice,
) -> float:
    score = 0.0
    for term in termos:
        freq = _freq_casada(indice, term, tf)
        idf = _idf_df(_df_casado(indice, term), n_docs)
        if freq:
            denom = freq + _K1 * (1.0 - _B + _B * dl / avgdl)
            if denom:
                score += idf * (freq * (_K1 + 1.0)) / denom
        if _esta_em(term, titulo, indice):
            score += _BONUS_TITULO * idf
    return score


def _truncar(texto: str, limite: int) -> str:
    compacto = texto.strip()
    if len(compacto) <= limite:
        return compacto
    corte = compacto[:limite].rsplit(" ", 1)[0].rstrip()
    if not corte:
        corte = compacto[:limite]
    return corte


def _num_artigo(artigo: str) -> int:
    achado = re.search(r"(\d+)", artigo)
    return int(achado.group(1)) if achado else 10**9


def _ranquear(pergunta: str, indice: _Indice) -> list[dict[str, str]]:
    originais, conteudo, ampliados = _termos_query(pergunta)
    if not conteudo:
        return []

    n_docs = indice.n_docs
    avgdl = indice.avgdl

    pontuados: list[tuple[float, float, int, int, dict[str, str]]] = []
    for artigo in indice.artigos:
        trecho = artigo.trecho
        tf = artigo.tf
        titulo = artigo.titulo
        dl = artigo.dl
        vocab = artigo.vocab
        casados = [term for term in conteudo if _termo_coberto(term, vocab, indice)]
        tempo_q = [t for t in conteudo if t in _TEMPO or _genero(t) in _TEMPO]
        if casados and tempo_q and (vocab & _TEMPO):
            for termo_tempo in tempo_q:
                if termo_tempo not in casados:
                    casados.append(termo_tempo)
        especificos = [term for term in casados if term not in _GENERICO and term not in _TEMPO and _genero(term) not in _TEMPO]
        score = _score_bm25(ampliados, tf, titulo, dl, avgdl, n_docs, indice)
        if tempo_q and (vocab & _TEMPO):
            score += _BONUS_TEMPO
        idf_sum = sum(_idf_df(_df_casado(indice, term), n_docs) for term in casados)
        pontuados.append((score, idf_sum, len(casados), len(especificos), trecho))

    pontuados.sort(key=lambda item: (-item[1], -item[0], _num_artigo(item[4]["artigo"])))
    pontuados = [
        item
        for item in pontuados
        if item[2] >= _MIN_TERMOS_CASADOS and item[3] >= 1 and item[1] >= _IDF_MIN
    ]
    if not pontuados:
        return []

    capitulo_top = pontuados[0][4]["capitulo"]
    escolhidos: list[dict[str, str]] = []
    usados = 0
    for _score, _idf, _n, _ne, trecho in pontuados:
        if trecho["capitulo"] != capitulo_top:
            continue
        resto = _MAX_CHARS - usados
        if resto < 40:
            break
        texto = _truncar(trecho["texto"], resto)
        if not texto:
            continue
        escolhidos.append(
            {
                "capitulo": trecho["capitulo"],
                "artigo": trecho["artigo"],
                "texto": texto,
            }
        )
        usados += len(texto)
        if len(escolhidos) >= _MAX_TRECHOS:
            break
    return escolhidos


def consultar_regulamento(
    pergunta: str,
    tool_context: object = None,
) -> dict[str, Any]:
    """Consulta trechos do regulamento interno relevantes à pergunta."""
    del tool_context
    try:
        path = caminho_regulamento()
        if not path.is_file():
            return {"status": STATUS_INDISPONIVEL}
        indice = _obter_indice()
        escolhidos = _ranquear(pergunta, indice)
        if not escolhidos:
            return {"encontrado": False, "trechos": []}
        return {"encontrado": True, "trechos": escolhidos}
    except Exception:
        logger.exception("falha de infraestrutura em consultar_regulamento")
        return {"status": STATUS_INDISPONIVEL}


def criar_tools_regulamento() -> list[FunctionTool]:
    """FunctionTool do ADK, sem confirmação; o modelo só vê `pergunta`."""
    return [FunctionTool(func=consultar_regulamento)]
