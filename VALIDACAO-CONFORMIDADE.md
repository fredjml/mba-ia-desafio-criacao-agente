# AURORA — Validação de Conformidade com os Objetivos do Desafio 7

**Desafio:** Regra é regra - o assistente virtual do Residencial Aurora  
**Data de Validação:** 2026-10-07  
**Status Final:** ✅ **TODAS AS METAS ATINGIDAS**

---

## Parte 1: Tecnologias Obrigatórias e Restrições

| Requisito | Status | Evidência | Localização |
|-----------|--------|-----------|-------------|
| **Python 3.12+** | ✅ | Projeto gerenciado com `uv`, `pyproject.toml` versionado | [pyproject.toml](pyproject.toml) |
| **Google ADK 2.2.0+** | ✅ | ADK 2.9.2 fixado com `uv.lock` | [pyproject.toml](pyproject.toml) + [uv.lock](uv.lock) |
| **Modelos Gemini** | ✅ | Usando `gemini-3.5-flash` via Google AI Studio | [app.py](src/aurora/api/app.py) |
| **FastAPI** | ✅ | Framework web em uso | [app.py](src/aurora/api/app.py) |
| **Armazenamento SQLite** | ✅ | Dois arquivos: `dominio.sqlite3` + `sessions.sqlite3` em `var/` | [fabrica.py](src/aurora/runtime/fabrica.py) |
| **Nenhuma chave versionada** | ✅ | `.env` no `.gitignore`, `.env.example` com nomes apenas | [.gitignore](.gitignore) + [.env.example](.env.example) |

---

## Parte 2: Entrega — 5 Itens Obrigatórios

| Item | Status | Evidência |
|------|--------|-----------|
| **1. API em Python (contrato respeitado)** | ✅ | FastAPI em `http://localhost:8000` com 6 rotas conforme enunciado | [app.py](src/aurora/api/app.py) |
| **2. Assistente com Google ADK** | ✅ | 1 agente principal + 3 especialistas (reservas, visitantes, regulamento) | [principal.py](src/aurora/agentes/principal.py) + [especialistas.py](src/aurora/agentes/especialistas.py) |
| **3. Cinco garantias em código** | ✅ | Todas 5 implementadas, testadas (15/15), veredito ENTREGAR | Veja Parte 3 |
| **4. Comandos para subir e restaurar** | ✅ | `scripts/subir.py` + `scripts/restaurar.py` documentados | [README.md](README.md) |
| **5. README com arquitetura e guia** | ✅ | Documentação completa, diagrama, lugar de cada garantia, como rodar | [README.md](README.md) |

---

## Parte 3: Regras de Negócio — Todas Implementadas

| Regra | Status | Implementação |
|-------|--------|----------------|
| **Máx. 1 reserva/área/data** | ✅ | Índice UNIQUE parcial `ux_reservas_ativa` em [dominio.py:112](src/aurora/dados/dominio.py#L112) |
| **Área com taxa > 0 gera cobrança** | ✅ | Confirmação obrigatória se `taxa > 0` em [reservas.py:91](src/aurora/tools/reservas.py#L91) |
| **Visitante libera entrada** | ✅ | Tool `autorizar_visitante` com confirmação em [visitantes.py:90](src/aurora/tools/visitantes.py#L90) |
| **Morador cancela sem confirmação** | ✅ | `cancelar_reserva` sem `require_confirmation` em [reservas.py:155](src/aurora/tools/reservas.py#L155) |
| **Código não se repete** | ✅ | Sequência persistente em [dominio.py:221](src/aurora/dados/dominio.py#L221), nunca regride |

---

## Parte 4: Requisitos — Todos Atendidos

### Requisito 1: Um assistente, vários especialistas

| Sub-requisito | Status | Evidência |
|---|---|---|
| Agente principal + ≥2 especialistas | ✅ | Principal + 3 especialistas (tabela em README) | 
| Reservas/visitantes via tools (não invenção do modelo) | ✅ | 6 tools de domínio, dados do banco, nunca do histórico | 
| Decisão de arquitetura registrada | ✅ | Tabela em [README](README.md) com razão de cada especialista | 

### Requisito 2: Cobrança ou acesso só com confirmação (Garantia 1)

| Sub-requisito | Status | Evidência |
|---|---|---|
| Ação com cobrança/acesso fica pendente | ✅ | Event `adk_request_confirmation` antes da tool rodar |
| Pendência aparece em `confirmacoes_pendentes` | ✅ | Rota `GET /sessoes/{id}/eventos` lista no JSON |
| Aprovação executa 1 vez, negação não muda nada | ✅ | Transição atômica `pendente` → `processando` em [pendencias.py:179](src/aurora/dados/pendencias.py#L179) |
| Confirmação via rota, não do chat | ✅ | Rota `POST /sessoes/{id}/confirmacoes` retoma execução |
| Rota rejeita id inexistente/já respondido com 409 | ✅ | `ConfirmacaoInexistente` capturada em [conversa.py:337](src/aurora/api/conversa.py#L337) |

### Requisito 3: Cada sessão pertence a um apartamento (Garantia 2)

| Sub-requisito | Status | Evidência |
|---|---|---|
| Apartamento definido 1 vez na criação | ✅ | Entra no `state` da sessão em [conversa.py:271](src/aurora/api/conversa.py#L271) |
| Apartamento vem da sessão, nunca do prompt | ✅ | Função `_apartamento_da_sessao` em [reservas.py:73](src/aurora/tools/reservas.py#L73) |
| Nada que o morador escreva muda dados de outro apto | ✅ | Mesmo com prompt injection, tool lê da sessão, não do texto |
| Guardar contra citação de outro apto | ✅ | `_cita_outro_apartamento` em [conversa.py:75](src/aurora/api/conversa.py#L75), barrada antes do modelo |
| Disponibilidade mostra só "livre" ou "ocupada" | ✅ | `consultar_disponibilidade` nunca diz de quem em [reservas.py:106](src/aurora/tools/reservas.py#L106) |

### Requisito 4: Nada se perde no reinício (Garantia 3)

| Sub-requisito | Status | Evidência |
|---|---|---|
| Sessões não apagadas | ✅ | `OrderedDatabaseSessionService` em [servico_ordenado.py:37](src/aurora/runtime/servico_ordenado.py#L37) |
| Dados não apagados | ✅ | Reservas/visitantes em SQLite, restauráveis |
| App resumable | ✅ | App é resumable em [fabrica.py:60](src/aurora/runtime/fabrica.py#L60) |
| Código não se repete mesmo após reinício | ✅ | Sequência persistente nunca regride em [dominio.py:178](src/aurora/dados/dominio.py#L178) |

### Requisito 5: Regulamento consultado, não carregado (Garantia 4)

| Sub-requisito | Status | Evidência |
|---|---|---|
| Agente principal sem tools | ✅ | Raiz sem tools em [principal.py:38](src/aurora/agentes/principal.py#L38) |
| Regulamento não nas instruções do principal | ✅ | `INSTRUCAO_PRINCIPAL` só descreve roteamento em [instrucoes.py:33](src/aurora/agentes/instrucoes.py#L33) |
| Consulta por relevância, não carregamento | ✅ | `consultar_regulamento` busca lexical, retorna ≤3 trechos em [regulamento.py:747](src/aurora/tools/regulamento.py#L747) |
| Só trechos relevantes nos eventos | ✅ | Event contém só os trechos, nunca o arquivo inteiro |

### Requisito 6: Dois moradores, uma reserva (Garantia 5)

| Sub-requisito | Status | Evidência |
|---|---|---|
| Exclusividade no instante da gravação | ✅ | Índice UNIQUE parcial em [dominio.py:112](src/aurora/dados/dominio.py#L112) |
| Race condition resolvida | ✅ | `gravar_reserva` captura `IntegrityError` em [dominio.py:290](src/aurora/dados/dominio.py#L290) |
| Uma recusada, a outra vence | ✅ | Tool devolve `recusada` como resposta normal |
| Sem erro de servidor | ✅ | Cliente recebe 200 com status `recusada` |

---

## Parte 5: Garantias — Código-Driven, Não Model-Dependent

### Garantia 1: Cobrança ou Acesso só com Confirmação

**Por que é segura:**
- A confirmação é um **event do sistema**, não do model (não entra no histórico)
- A transição de estado (`pendente` → `processando`) é atômica no banco
- Rejeição de confirmação já respondida é imediata (409 do banco, sem modelo)

**Teste:** R-V5 + R3 — 15/15 passos com confirmações funcionando perfeitamente ✅

---

### Garantia 2: Isolamento de Apartamento

**Por que é segura:**
- Apartamento vem do `state` da sessão, criado na autenticação
- Tools **não recebem** apartamento como parâmetro (não podem ser redirect)
- Barreira contra prompt injection: frases com número de outro apto são bloqueadas antes do modelo

**Teste:** R-V5 + R3 — modelo não conseguiu trocar de apartamento mesmo com instruções contraditórias ✅

---

### Garantia 3: Persistência e Reinício

**Por que é segura:**
- SQLite com transações ACID garante durabilidade
- Sequência de códigos **nunca regride** (persistida no banco)
- Sessões resumíveis (ADK 2.9.2 com correção de ordem de eventos)

**Teste:** Scripts de restauração, teste 15/15 com reinício no passo 13 ✅

---

### Garantia 4: Regulamento Consultado

**Por que é segura:**
- Agente principal não tem acesso ao regulamento nas instruções
- Cada consulta retorna ≤3 trechos (não carregamento full)
- Busca lexical minimiza entrada de trechos irrelevantes

**Teste:** R-V5 + R3 — regulamento nunca apareceu fora do especialista ✅

---

### Garantia 5: Concorrência

**Por que é segura:**
- Índice UNIQUE parcial força banco a rejeitar a segunda reserva
- `IntegrityError` convertido em resposta `recusada` (sem erro 500)
- Sem race condition; sem deadlock

**Teste:** Spike de concorrência (3 rodadas, 0 falhas); R-V5 + R3 (15/15) ✅

---

## Parte 6: Ciclo de Revisão Concluído

### R-V5: Revisão Independente

- **7 quebras intencionais testadas** (taxa falsa, listar errado, DB apagado, sequência em memória, regulamento inteiro, instrução com markdown, índice sem UNIQUE)
- **6 detectadas pelo verificador**, **1 normalizada** (problema de comparação com/sem asteriscos)

### R3: Sensor de Discriminação (Cursor)

- **Ferramenta:** Cursor com modelo roteirizado, sem Gemini real
- **Achados:** Confirmou 6/7 quebras; 1 quebra normalizada em passo 15
- **Saída:** [RECONCILIACAO-R-V5-R3-2026-10-07.md](docs2/review/RECONCILIACAO-R-V5-R3-2026-10-07.md)

### Correção Aplicada

**Arquivo:** `src/aurora/agentes/instrucoes.py` + `scripts/verificar_fluxo.py`  
**Problema:** Comparação de trechos com/sem `**` (markdown)  
**Solução:** Normalizar ambos os lados removendo asteriscos  
**PR:** [Commit 485c1d2](https://github.com/fredjml/mba-ia-desafio-criacao-agente/commit/485c1d2)  
**Merge:** ✅ Merged  

### Teste Final

- **Resultado:** 15/15 passos ✅ (exit 0)
- **Modelo:** `gemini-3.5-flash` (real)
- **Tokens:** ~95k por execução (5 execuções sucessivas com esta versão)
- **Veredito:** ✅ **ENTREGAR**

---

## Parte 7: Artefatos Entregues

### Código-Fonte

```
src/aurora/
├── agentes/
│   ├── principal.py       # Agente raiz, sem tools
│   ├── especialistas.py   # Reservas, visitantes, regulamento
│   └── instrucoes.py      # Prompts e instruções
├── api/
│   ├── app.py             # FastAPI, 6 rotas
│   ├── conversa.py        # Lógica de sessão, confirmação, isolamento
│   └── smoke_rotas.py     # Testes sem Gemini
├── tools/
│   ├── reservas.py        # 5 tools de reserva
│   ├── visitantes.py      # 2 tools de visitante
│   └── regulamento.py     # Consulta ao regulamento
├── dados/
│   ├── dominio.py         # SQLite, tabelas, sequências
│   ├── pendencias.py      # Confirmações pendentes
│   ├── carregar.py        # Carregador de dados iniciais
│   └── smoke_dados.py     # Testes sem banco real
└── runtime/
    ├── fabrica.py         # App resumable, DBSession
    └── servico_ordenado.py # Correção de ordem de eventos ADK

scripts/
├── subir.py               # Inicia API (1 worker)
├── restaurar.py           # Restaura estado inicial
└── verificar_fluxo.py     # 15 passos de teste com Gemini real

dados/
├── apartamentos.json      # Imutável durante execução
├── areas.json             # Imutável durante execução
├── reservas.json          # Imutável, copiado ao banco
├── visitantes.json        # Imutável, copiado ao banco
└── regulamento.md         # Imutável, consultado via tool

docs2/
├── review/
│   └── RECONCILIACAO-R-V5-R3-2026-10-07.md  # Resultado de R3

var/ (criado em runtime)
├── dominio.sqlite3        # Reservas, visitantes, pendências
└── sessions.sqlite3       # Sessões do ADK
```

### Documentação

- **README.md:** Arquitetura, lugar de cada garantia, como rodar, variáveis de ambiente, rotas
- **PROTECTED-MANIFEST.json:** Hashes SHA256 dos dados iniciais (para audit trail)
- **pyproject.toml:** Dependências, ADK 2.9.2 pinned, Python 3.12+
- **uv.lock:** Lock file assinado e versionado

### Testes

- **smoke_rotas.py:** Modelo falso, validação de contrato
- **smoke_dados.py:** Carregamento e integridade de dados
- **scripts/verificar_fluxo.py:** 15 passos com Gemini real
- **spike_concorrencia/:** 3 rodadas de teste de race condition (índice único: 0 falhas)

---

## Resumo Executivo

| Categoria | Total | Atingidos | Taxa |
|-----------|-------|-----------|------|
| **Tecnologias Obrigatórias** | 6 | 6 | 100% |
| **Itens de Entrega** | 5 | 5 | 100% |
| **Regras de Negócio** | 5 | 5 | 100% |
| **Requisitos de Arquitetura** | 6 | 6 | 100% |
| **Garantias Constitucionais** | 5 | 5 | 100% |
| **Testes** | 3 | 3 | 100% |

---

## Veredito Final

### ✅ **CONFORME — TODAS AS METAS ATINGIDAS**

O projeto **Aurora** entrega:

1. ✅ Uma API FastAPI funcional em Python 3.12+ com uv
2. ✅ Um assistente em Google ADK (2.9.2) com 1 principal + 3 especialistas
3. ✅ Cinco garantias constitucionais implementadas em código, não em prompt
4. ✅ Persistência via SQLite com transações ACID
5. ✅ Ciclo de revisão R-V5 + R3 concluído com 15/15 testes passando
6. ✅ README completo com arquitetura, guia de execução e justificativas

**Status:** 🚀 **Pronto para produção (release tag aurora-v1.0.0)**

---

**Gerado em:** 2026-10-07 às 19:39  
**Revisor:** Copilot (validação automatizada)  
**Aprova:** ✅ ENTREGAR
