# Arquitetura — Aurora Virtual Assistant

**Visão geral da arquitetura de sistema**, componentes principais, fluxos e tecnologias.

---

## 🏗️ Diagrama de Alto Nível

```
┌─────────────────────────────────────────────────────────────┐
│                      CAMADA DE APRESENTAÇÃO                 │
│              (Slack, Web, Aplicativo Residencial)           │
└──────────────────────────┬──────────────────────────────────┘
                           │
                    ┌──────▼──────┐
                    │   FastAPI   │
                    │   servidor  │ (porta 8000)
                    └──────┬──────┘
                           │
      ┌────────────────────┼────────────────────┐
      │                    │                    │
┌─────▼────────┐  ┌────────▼────────┐  ┌───────▼───────┐
│  Agente ADK  │  │   Ferramentas   │  │  Regulamento  │
│  Principal   │  │   (5 tools)     │  │    (Query)    │
│              │  │                 │  │               │
│ • Fluxo      │  │ • reservas      │  │ • Consultar   │
│ • Confirmação│  │ • visitantes    │  │   (≤3 trechos)│
│ • Acomodação │  │ • histórico     │  │ • Sem escrita │
└──────┬───────┘  │ • preferências  │  └───────┬───────┘
       │          │ • autorização   │          │
       │          └────────┬────────┘          │
       │                   │                   │
       └───────────────────┼───────────────────┘
                           │
                    ┌──────▼──────────┐
                    │   Persistência  │
                    │   SQLite (WAL)  │
                    └─────────────────┘
```

---

## 🔧 Componentes Principais

### 1. **Agente ADK Principal** (`src/aurora/agentes/`)

**Responsabilidade:** Orquestrar fluxo de confirmação, gerenciar sessões, respeitar regulamento.

- **Instrução** (`instrucoes.py`): Prompt único, sem tools de regulamento
- **Estado** (`memoria.py`): Confirmações pendentes, acomodações solicitadas
- **Isolamento:** Apartamento extraído do contexto de sessão (não de parâmetros do usuário)

### 2. **Ferramentas Especializadas** (`src/aurora/tools/`)

| Ferramenta | Garantia | Responsabilidade |
|------------|----------|------------------|
| `reservas.py` | ✅ Confirmação | CRUD de reservas com lógica de taxa |
| `visitantes.py` | ✅ Isolamento | Autorização de visitantes por apartamento |
| `historico.py` | ✅ Reinício | Resumo de eventos para contexto |
| `preferencias.py` | ✅ Concorrência | Configurações por apartamento (thread-safe) |
| `autorizacao.py` | ✅ Isolamento | Regras de autorização (não acessa passagem de parâmetros) |

### 3. **Consulta de Regulamento** (`src/aurora/tools/regulamento.py`)

**Responsabilidade:** Retornar trechos relevantes do regulamento **apenas** (sem poder de escrita).

- Procura lexical por keywords (não LLM-driven)
- Retorna até 3 trechos ordenados por relevância
- Resposta estruturada: `{"encontrado": bool, "trechos": [...]}`

### 4. **Persistência** (`src/aurora/dados/`)

| Módulo | Responsabilidade | Garantias |
|--------|-----------------|-----------|
| `dominio.py` | Tabelas, índices, sequências | Código persistente, ACID, UNIQUE parcial |
| `repositorio.py` | CRUD com contexto de sessão | Isolamento por apartamento |
| `fabrica.py` | Gerenciamento de conexões | Instâncias isoladas por sessão |

---

## 🔄 Fluxo de Requisição

```
1. Usuário envia mensagem via Slack/HTTP
   │
2. FastAPI: POST /chat (session_id, message, apartamento)
   │
3. ADK recupera sessão + contexto de histórico
   │
4. Agente processa com ferramentas:
   │
   ├─► reservas.criar() → confirma nova reserva?
   │
   ├─► visitantes.autorizar() → visitante permitido?
   │
   ├─► regulamento.consultar() → que diz o manual?
   │
   └─► histórico.resumir() → contexto de eventos anteriores
   │
5. Transação no banco (todas as tools rodam no mesmo contexto)
   │
6. Resposta retorna ao usuário com confirmação de ação
```

---

## 🗄️ Modelo de Sessão

**Ciclo de vida:**

```
┌─────────────────┐
│  Sessão Criada  │
│  (session_id)   │
└────────┬────────┘
         │
    [Histórico]
    [Confirmações Pendentes]
    [Acomodações Solicitadas]
         │
    ┌────▼────┐
    │ 1+ Msgs │
    └────┬────┘
         │
    [Transações ADK]
    [Atualiza Estado]
         │
    ┌────▼────────┐
    │ Sessão Fica │
    │ Sem Msgs?   │
    └────┬───┬────┘
         │   │
         │   └─► Recuperável (histórico + context)
         │
         └─► Deletada (timeout TTL)
```

**Storage:** `OrderedDatabaseSessionService` (ADK)
- Cada sessão = fila de transações no banco
- Recuperável após crash/reinício
- Isolada por `session_id` + `apartment` do contexto

---

## ⚙️ Tecnologias Aplicadas

| Stack | Escolha | Por quê |
|-------|---------|--------|
| **Runtime** | Python 3.12+ | ADK apenas |
| **Web** | FastAPI | Type-safe, async, documentação automática |
| **Banco** | SQLite (WAL) | ACID, embarcado, sem deps externas |
| **Build** | `uv` | Reprodutível, rápido |
| **IA** | Google Gemini ADK | Agents com orchestração nativa |
| **Sessões** | ADK Session Service | Persistência de estado automática |

---

## 🔐 5 Garantias Constitucionais

Cada garantia é **code-driven**, não dependente de comportamento do modelo:

### ✅ Confirmação
- **Regra:** Cada ação (reserva, autorização) requer confirmação explícita
- **Implementação:** Transação atômica em `reservas.confirmar()`; 409 Conflict se duplicada
- **Código:** [src/aurora/tools/reservas.py:100-115](../../src/aurora/tools/reservas.py)

### ✅ Isolamento
- **Regra:** Apartamento extraído do contexto de sessão, nunca de parâmetros de usuário
- **Implementação:** Guarda lexical contra prompt injection em `memoria.py`
- **Código:** [src/aurora/agentes/memoria.py:15-25](../../src/aurora/agentes/memoria.py)

### ✅ Reinício
- **Regra:** Sequência de codes nunca regride; app recuperável
- **Implementação:** `UPDATE ... RETURNING` na tabela de sequências + SQLite ACID
- **Código:** [src/aurora/dados/dominio.py:200-225](../../src/aurora/dados/dominio.py)

### ✅ Regulamento
- **Regra:** Agente principal não tem ferramenta de escrita no regulamento
- **Implementação:** `consultar_regulamento()` retorna até 3 trechos lexicais (readonly)
- **Código:** [src/aurora/tools/regulamento.py:50-80](../../src/aurora/tools/regulamento.py)

### ✅ Concorrência
- **Regra:** Sem double-booking; máximo 1 reserva ativa por (área, data)
- **Implementação:** `CREATE UNIQUE INDEX ux_reservas_ativa ON reservas(area, data) WHERE ativa=1`
- **Código:** [src/aurora/dados/dominio.py:112](../../src/aurora/dados/dominio.py)

---

## 📊 Estrutura de Diretórios

```
mba-ia-desafio-criacao-agente/
├── src/
│   └── aurora/
│       ├── agentes/          # ADK agent + instrucoes
│       ├── tools/            # 5 ferramentas especializadas
│       ├── dados/            # Persistência + schema
│       └── runtime/          # FastAPI + servir
├── tests/
│   ├── integracao/           # Fluxos fim-a-fim
│   └── unitario/             # Testes de tools isoladas
├── scripts/
│   ├── verificar_fluxo.py    # Teste de conformidade (15 passos)
│   └── servidor_roteado.py   # Servidor com modelo mock
├── dados/
│   ├── regulamento.md        # Arquivo único de regras
│   └── residencial.db        # Banco SQLite (criado em runtime)
├── docs2/                    # Documentação estruturada (NEW)
│   ├── INDEX.md              # Este índice
│   ├── architecture/         # 4 docs técnicos
│   ├── design/               # 3 docs de decisão
│   ├── review/               # Ciclo de validação
│   └── plan/prompts/         # Prompts de revisão
├── .github/
│   ├── workflows/            # CI/CD (se houver)
│   └── modernize/            # Planos de modernização
├── README.md                 # Documentação principal
└── VALIDACAO-CONFORMIDADE.md # Compliance matriz (NEW)
```

---

## 🚀 Execução

### Desenvolvimento

```bash
uv sync                                    # Instalar deps
python -m aurora.runtime.servidor 8000    # Rodar servidor
```

### Verificação (Conformidade)

```bash
python -X utf8 -u scripts/verificar_fluxo.py \
  --subir -v --subir-cmd "<caminho>/python servidor_roteado.py"
# Esperado: 15/15 passos passam
```

### Produção

```bash
uvicorn aurora.runtime.app:app --host 0.0.0.0 --port 8000
```

---

## 📖 Mais Informações

- **Dados:** Ver [data-model.md](./data-model.md)
- **APIs:** Ver [api-contracts.md](./api-contracts.md)
- **Segurança:** Ver [security-model.md](./security-model.md)
- **Decisões:** Ver [../design/decisions.md](../design/decisions.md)

---

**Última atualização:** 2026-10-07  
**Status:** ✅ Completo
