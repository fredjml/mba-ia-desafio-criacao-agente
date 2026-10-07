# Decisões Arquiteturais — Aurora Virtual Assistant

**Por que Python/FastAPI/SQLite, trade-offs, alternativas consideradas.**

---

## 🎯 Decisão 1: Python + ADK (Microsoft Adaptive Agents Development Kit)

### Escolhido
```
Python 3.12+ (mínimo para ADK 2024+)
+ Microsoft ADK (OrderedDatabaseSessionService, agentes estruturados)
```

### Trade-offs

| Aspecto | Vantagem | Desvantagem |
|--------|----------|------------|
| **Type Safety** | Pydantic valida em runtime | Não compile-time (só mypy offline) |
| **Performance** | Adequado para assistentes (I/O-bound) | Não para processamento de alta CPU |
| **Deployment** | Containerizado com uvicorn | Precisa Python 3.12+ no host |
| **Ecosystem** | Gemini API oficial, bibliotecas ricas | Menos de empresa vs Node.js |

### Alternativas Consideradas e Rejeitadas

#### ❌ TypeScript/Node.js
```
Razão: ADK é Python-first; suporte TypeScript é secundário
Risco: Quebras de compatibilidade no ADK entre versões
Vantagem que perde: Homogeneidade frontend-backend (não aplicável aqui)
```

#### ❌ Java/Spring Boot
```
Razão: Overhead de JVM desnecessário para assistente stateless
Risco: Consumo de memória maior, startup lento
Vantagem que perde: Type safety (já temos Pydantic)
```

#### ❌ Go
```
Razão: ADK só tem suporte oficial em Python/Node.js
Risco: Implementação manual de sessão persistente = duplicação
```

### Decisão Final
✅ **Python 3.12 + ADK** — alinhado com estratégia de assistentes Azure, sem risco de incompatibilidade.

---

## 🎯 Decisão 2: FastAPI (não Django REST / GraphQL)

### Escolhido
```
FastAPI 0.104+
- Type hints nativas (Pydantic schemas)
- Async/await out-of-the-box
- OpenAPI automático
- Validação declarativa
```

### Trade-offs

| Aspecto | FastAPI | Django REST | GraphQL |
|--------|---------|------------|---------|
| **Setup** | 20 linhas | 100+ linhas | 50+ linhas |
| **Async** | ✅ Nativo | ⚠️ Async_to_sync | ✅ Sim |
| **Validação** | ✅ Pydantic automático | ✅ Serializers | ❌ Schema language |
| **Docs** | ✅ OpenAPI automático | ⚠️ Swagger manual | ⚠️ GraphQL UI |
| **Queryability** | REST simples | ✅ Customizável | ✅ Muito flexível |

### Por que não GraphQL?
```
1. Clientes são intencionais (chat + confirmação)
2. Over-fetching não é problema (apto já sabe seus dados)
3. GraphQL adiciona complexidade sem valor para assistentes
4. Edge case: upload de arquivo (multipart/form-data) é complicado em GraphQL
```

### Por que não Django REST?
```
1. ORM (Django) vs raw SQL: Aqui precisamos atomicidade transacional,
   ADK OrderedDatabaseSessionService espera DB nativo
2. Boilerplate de serializers: FastAPI resolve com type hints
3. Async é second-class citizen no Django clássico
```

### Decisão Final
✅ **FastAPI** — velocidade de desenvolvimento + validação automática + compatibilidade com ADK.

---

## 🎯 Decisão 3: SQLite (não PostgreSQL / MongoDB)

### Escolhido
```
SQLite 3.37+ (WAL mode)
- Transações ACID nativas
- Sem dependência externa
- Backup = 1 arquivo
- Replicação simples (cp-based)
```

### Trade-offs

| Aspecto | SQLite | PostgreSQL | MongoDB |
|--------|--------|-----------|---------|
| **Concorrência** | ✅ WAL (readers não bloqueiam) | ✅✅ MVCC | ❌ Documentos grandes |
| **Setup** | ✅ Nenhum | ❌ Servidor externo | ❌ Servidor externo |
| **Scale** | ✅ Até ~10M linhas (8 MB) | ✅✅ Ilimitado | ✅ Ilimitado |
| **Backup** | ✅ 1 arquivo | ⚠️ pg_dump necessário | ⚠️ Snapshots |
| **UNIQUE INDEX Parcial** | ✅ Suportado | ✅ Suportado | ❌ Não |
| **Isolation Level** | ✅ SERIALIZABLE | ✅ READ COMMITTED | ⚠️ Document-level |

### Por que não PostgreSQL?
```
1. Escalabilidade: 30 dias × 100 apts = ~500 reservas/visitantes/eventos
   = 1-2 MB dados + 1 MB índices = 3 MB total
   SQLite é confortável até 100 MB
   
2. Operational: Requer DBA + backup externo
   ADK OrderedDatabaseSessionService espera arquivo local
   PostgreSQL remoto = latência + complexidade de replica

3. Garantia 5 (Concorrência): UNIQUE INDEX parcial (ativa=1)
   Postgres suporta, mas SQLite é mais simples de manter
```

### Por que não MongoDB?
```
1. Documentos aninhados: Aqui temos tabelas bem-definidas
   Flexibility do MongoDB = sem ACID garantido para multi-doc
   
2. Índice único parcial: MongoDB não suporta tão bem
   Queremos MAX 1 reserva ativa por (área, data)
   Precisamos UNIQUE INDEX WHERE ativa=1
   
3. Queries: Operações são SELECT simples
   SQL é mais natural que agregações do Mongo
```

### Decisão Final
✅ **SQLite 3.37+ WAL** — sem dependências externas, 5 garantias implementáveis, backup trivial (1 arquivo).

---

## 🎯 Decisão 4: ADK OrderedDatabaseSessionService (não Redis / Memcache / JWT)

### Escolhido
```
Microsoft ADK OrderedDatabaseSessionService
- Sessão persistida em banco (SQLite)
- Histórico de turno = replay seguro
- Recuperação automática de crash
- Integrado com agentes ADK
```

### Trade-offs

| Aspecto | ADK + DB | Redis | JWT Stateless | In-Memory |
|--------|----------|-------|--------------|-----------|
| **Persistência** | ✅ Crash-safe | ⚠️ Memória volátil | ❌ Perdido | ❌ Perdido |
| **Recovery** | ✅ Automático | ❌ Reexecution | ❌ Reexecution | ❌ Reexecution |
| **TTL** | ✅ Explícito (SQL) | ✅ Nativo | ✅ exp claim | ✅ Código |
| **Size Limit** | ✅ Escala com DB | ⚠️ Ram host | ✅ Ilimitado | ⚠️ Ram host |
| **Auditoria** | ✅ INSERT histórico | ❌ Logs externos | ❌ Sem histórico | ❌ Sem histórico |

### Por que não Redis?
```
1. Garantia 3 (Reinício): Sessão em Redis morre com processo
   Precisamos replay dos eventos = histórico durável
   
2. Backup: Redis snapshots podem perder segundos de dados
   Aqui confirmação é instantânea (ACID)
   
3. Custo: Redis node adicional + monitoramento
   SQLite local = 0 dependências operacionais
```

### Por que não JWT Stateless?
```
1. Garantia 3 (Reinício): User bate na API, sessão perdida
   "Recupere seu histórico" = UX ruim
   
2. Audit: Sem registro persistente de transições de estado
   Regulamento pode exigir auditoria (condomínio)
   
3. Cancelamento: Não conseguimos revogar token em tempo real
   (Token válido até expirar mesmo se reserva for cancelada)
```

### Por que não In-Memory?
```
1. Mesma razão que Redis: volatilidade
2. Múltiplos processos (gunicorn workers) = estado inconsistente
```

### Decisão Final
✅ **ADK OrderedDatabaseSessionService** — Garantia 3 (Reinício) requer persistência + replay + histórico auditável.

---

## 🎯 Decisão 5: Lexical Search (não LLM embedding search)

### Escolhido
```
Busca por keyword no regulamento.md
- Split linhas
- Match determinístico
- Rank por frequência de ocorrência
- Retorna top 3
```

### Trade-offs

| Aspecto | Lexical | Embedding/Vector | Semantic LLM |
|--------|---------|------------------|--------------|
| **Consistência** | ✅ 100% determinístico | ⚠️ Varia com modelo | ⚠️ Varia com prompt |
| **Latência** | ✅ <10ms | ⚠️ 100-500ms | ⚠️ 500ms+ (Gemini) |
| **Custo** | ✅ Grátis | ⚠️ Embedding API | ⚠️ LLM tokens |
| **Hallucination** | ✅ Impossível | ⚠️ Possível | ⚠️ Possível |
| **UX: accuracy** | ⚠️ Typos quebram | ✅ Typo-robust | ✅ Typo-robust |

### Por que não Embedding Vector DB?
```
Razão 1: Garantia 4 (Regulamento) exige determinismo
         "Art. 1º" deve sempre retornar o mesmo resultado
         Embeddings de Gemini podem mudar se modelo atualizar
         
Razão 2: Custo operacional
         Embedding API = mais chamadas, mais tokens
         Lexical search = 0 dependências
         
Razão 3: Auditoria
         Qual busca levou a qual resultado?
         Com embedding: obscuro (vetor de 768 dimensões)
         Com lexical: log simples ("procurou 'taxa', achou linha 42")
```

### Por que não Semantic LLM Search?
```
Razão 1: Dupla LLM call
         User → Agente entende intent → Agente consulta regulamento
         Se regulamento também chamar Gemini = 2 RTTs, mais tokens
         
Razão 2: Hallucination
         LLM pode "resumir" incorretamente um artigo
         Usuário confia em Aurora = responsabilidade de exatidão
         
Razão 3: Versionamento
         Regulamento v2 → Embeddings precisam recompute
         Lexical = nenhum overhead
```

### Quando UX sofre
```
User: "Quanto é a taxa para o salão?"
Lexical: Procura por "taxa", acha 2 linhas de "taxa de fundo"
Problem: Não encontra "taxa" se escrito como "tarifa" no regulamento

Solução: User repete com outra palavra ou Agente sugere
"Não encontrei 'taxa'; tente 'tarifa' ou 'cobrança'"
```

### Decisão Final
✅ **Lexical search** — Garantia 4 (Regulamento) requer determinismo + auditabilidade. UX pode degradar levemente, mas segurança e compliance ganham.

---

## 📊 Resumo de Decisões

| Decisão | Escolhido | Razão Principal | Risco Mitigado |
|---------|-----------|-----------------|----------------|
| Linguagem | Python + ADK | ADK é Python-first | Incompatibilidade com framework |
| Framework Web | FastAPI | Type hints + Async | Boilerplate Django |
| Banco de Dados | SQLite WAL | Zero dependências + ACID | Setup operacional |
| Sessão | ADK + OrderedDatabaseSessionService | Garantia 3 (Reinício) | Perda de estado |
| Busca | Lexical | Garantia 4 (determinismo) | Hallucination LLM |

---

## 🔮 Possíveis Evoluções

Se o projeto escalar:

1. **SQLite → PostgreSQL**
   - Quando: >1M linhas de dados
   - Como: Migração SQL simples (esquema é compatível)
   - Quando não: Enquanto caber em 100 MB SQLite

2. **FastAPI → Kubernetes**
   - Quando: >5 apts, múltiplos processos
   - Como: Docker + K8s com session DB compartilhado
   - Quando não: Enquanto FastAPI + uvicorn + 1 processo basta

3. **Lexical → Vector Search**
   - Quando: Regulamento >10k linhas + UX de typo-tolerance crítica
   - Como: Adicionar Pinecone/Weaviate com fallback lexical
   - Quando não: Enquanto lexical cobre os queries

---

**Última atualização:** 2026-10-07  
**Status:** ✅ Decisões documentadas e justificadas
