# Modelo de Dados — Aurora Virtual Assistant

**Schema SQL, tabelas, índices, sequências e relacionamentos.**

---

## 📋 Tabelas

### 1. `reservas`

**Responsabilidade:** Armazenar todas as reservas de áreas comuns.

```sql
CREATE TABLE reservas (
    id TEXT PRIMARY KEY,                      -- RSV-#### gerado
    apartamento TEXT NOT NULL,                 -- 101-305
    area TEXT NOT NULL,                        -- 'salao-de-festas', 'churrasqueira'
    data TEXT NOT NULL,                        -- YYYY-MM-DD
    horario_inicio TEXT NOT NULL,              -- HH:MM
    horario_fim TEXT NOT NULL,                 -- HH:MM
    descricao TEXT,                            -- Para qual evento
    taxa DECIMAL(10, 2),                       -- Valor cobrado (se houver)
    confirmada BOOLEAN DEFAULT FALSE,          -- User confirmou?
    ativa BOOLEAN DEFAULT TRUE,                -- Não cancelada?
    criada_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    atualizada_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Índices:**

```sql
CREATE UNIQUE INDEX ux_reservas_ativa 
  ON reservas(area, data) WHERE ativa=1;
  -- Garante máx 1 reserva ATIVA por (área, data)

CREATE INDEX ix_reservas_apto 
  ON reservas(apartamento);

CREATE INDEX ix_reservas_ativa_apto 
  ON reservas(apartamento, ativa);
```

**Fluxo de estado:**

```
┌─────────────────┐
│ Criada          │ confirmada=F, ativa=T
│ (nova reserva)  │
└────────┬────────┘
         │
    User confirma?
         │
    ┌────▼─────┐
    │ SIM   NÃO │
    │           │
┌───▼──────┐  ┌─────▼──────┐
│Confirmada│  │ Cancelada   │
│TA=T      │  │ ativa=F     │
└───┬──────┘  └─────────────┘
    │
    └─► (fim de vida — nunca muda de novo)
```

---

### 2. `visitantes`

**Responsabilidade:** Registro de autorizações de visitantes.

```sql
CREATE TABLE visitantes (
    id TEXT PRIMARY KEY,                      -- VIS-#### gerado
    apartamento TEXT NOT NULL,                -- Quem autorizou
    nome TEXT NOT NULL,                       -- Nome do visitante
    rg TEXT UNIQUE NOT NULL,                  -- Identificação
    autorizado BOOLEAN DEFAULT FALSE,         -- Status de autorização
    criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Índices:**

```sql
CREATE INDEX ix_visitantes_apto ON visitantes(apartamento);
CREATE UNIQUE INDEX ux_visitantes_rg_apto 
  ON visitantes(rg, apartamento);
  -- Cada apartamento autoriza cada visitante uma vez
```

---

### 3. `historico_eventos`

**Responsabilidade:** Audit log de todas as ações (para resumo de contexto).

```sql
CREATE TABLE historico_eventos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,                 -- Qual sessão gerou
    apartamento TEXT NOT NULL,                -- Contexto de quem
    tipo_evento TEXT NOT NULL,                -- 'reserva_criada', 'visitante_autorizado'
    dados_evento JSON,                        -- Estrutura completa do evento
    criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Valores esperados de `tipo_evento`:**
- `reserva_criada`
- `reserva_confirmada`
- `reserva_cancelada`
- `visitante_autorizado`
- `visitante_revogado`
- `preferencia_atualizada`
- `consulta_regulamento`

---

### 4. `preferencias`

**Responsabilidade:** Configurações por apartamento (ex: horário de silêncio).

```sql
CREATE TABLE preferencias (
    id TEXT PRIMARY KEY,
    apartamento TEXT NOT NULL UNIQUE,        -- 1 prefs por apto
    horario_silencio_inicio TEXT,            -- HH:MM
    horario_silencio_fim TEXT,               -- HH:MM
    avisar_confirmacao BOOLEAN DEFAULT TRUE, -- Notificar quando confirmar
    criada_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    atualizada_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

### 5. `sequencias`

**Responsabilidade:** Contadores monotônicos para IDs (não regride após reinício).

```sql
CREATE TABLE sequencias (
    nome TEXT PRIMARY KEY,                    -- 'reserva', 'visitante'
    valor INTEGER NOT NULL,                  -- Próximo valor a usar
    atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Inserção inicial:**

```sql
INSERT INTO sequencias (nome, valor) VALUES
  ('reserva', 4820),
  ('visitante', 1000);
```

**Garantia de reinício:**

```python
# Em dominio.py
cursor.execute("""
    UPDATE sequencias SET valor = valor + 1 
    WHERE nome = ?
    RETURNING valor
""", ('reserva',))
novo_id = cursor.fetchone()[0]
# SQLite ACID garante que valor nunca regride
```

---

## 🔑 Relacionamentos

```
┌─────────────────┐
│   reservas      │
│   (muitas)      │
└────────┬────────┘
         │ apartamento
         │
┌────────▼────────────┐
│  (Contextual)       │
│  apartamento STRING │
└─────────────────────┘
         △
         │ (extraído de session context)
         │
    [Nunca de parâmetro de user]

┌─────────────────┐
│  visitantes     │
│   (muitos)      │
└────────┬────────┘
         │ apartamento
         │
    [Mesmo contexto]

┌──────────────────┐
│  preferencias    │
│   (um por apto)  │
└──────┬───────────┘
       │ apartamento
       │
   [Chave única]
```

---

## 📊 Estado Exemplo (Dados Iniciais)

### Cenário: Apartamento 101, Sessão "abc123"

**Sequências:**
```sql
INSERT INTO sequencias (nome, valor) VALUES ('reserva', 4820);
```

**Primeira ação: Criar reserva**
```sql
-- Gera: RSV-4821
-- Inserir em reservas com confirmada=FALSE
INSERT INTO reservas (
    id, apartamento, area, data, horario_inicio, horario_fim, 
    descricao, taxa, confirmada, ativa
) VALUES (
    'RSV-4821', '101', 'salao-de-festas', '2030-03-16',
    '14:00', '18:00', 'Festa aniversário', 150.00,
    FALSE, TRUE
);
```

**Histórico registra:**
```sql
INSERT INTO historico_eventos (
    session_id, apartamento, tipo_evento, dados_evento
) VALUES (
    'abc123', '101', 'reserva_criada',
    '{"reserva_id": "RSV-4821", "area": "salao-de-festas", "data": "2030-03-16"}'
);
```

---

## 🔄 Transações

### Criação de Reserva (Atômica)

```python
with db.transaction():
    # 1. Incrementar sequência
    novo_id = db.execute("""
        UPDATE sequencias SET valor = valor + 1 
        WHERE nome = 'reserva'
        RETURNING valor
    """).scalar()
    
    # 2. Inserir reserva
    db.execute("""
        INSERT INTO reservas (id, apartamento, ...) 
        VALUES (?, ...)
    """, (f'RSV-{novo_id}', apartamento, ...))
    
    # 3. Registrar evento
    db.execute("""
        INSERT INTO historico_eventos (...)
        VALUES (...)
    """)
    
    # Se falhar em qualquer ponto: rollback automático
```

### Confirmação de Reserva

```python
with db.transaction():
    # Atualiza confirmada=TRUE
    db.execute("""
        UPDATE reservas SET confirmada = TRUE 
        WHERE id = ? AND apartamento = ?
    """)
    
    # Registra evento
    db.execute("""
        INSERT INTO historico_eventos (...)
        VALUES ('reserva_confirmada', ...)
    """)
```

---

## 🚨 Constraints Implementadas

| Constraint | Nível | Propósito |
|-----------|-------|----------|
| `UNIQUE(id)` | Tabela | Sem IDs duplicados |
| `UNIQUE INDEX ux_reservas_ativa` | Índice parcial | **Máx 1 reserva ativa por (área, data)** ← Concorrência |
| `UNIQUE INDEX ux_visitantes_rg_apto` | Índice | Sem autorização duplicada |
| `NOT NULL` | Coluna | Campos obrigatórios |
| `DEFAULT` | Coluna | Timestamps, booleanos |
| **SQLite ACID** | Transação | Todas atualizam atomicamente |

---

## 📈 Crescimento Esperado

| Tabela | 30 dias | 1 ano |
|--------|---------|-------|
| `reservas` | ~300 (10/dia) | ~3.650 |
| `visitantes` | ~150 | ~1.800 |
| `historico_eventos` | ~600 | ~7.300 |
| `preferencias` | ~40 (estático) | ~40 |
| **Tamanho DB** | ~1 MB | ~8 MB |

SQLite suporta confortavelmente até 10GB com WAL mode ativo.

---

## ⚡ Performance

- **Lookups por apartamento:** O(log N) com `ix_reservas_apto`
- **Verificação de conflito:** O(1) com índice parcial único
- **Histórico completo:** O(log N) com `session_id`
- **Escrita:** Async via SQLite WAL (não bloqueia leitura)

---

## 🔗 Referências

- **Inicialização:** [src/aurora/dados/dominio.py](../../src/aurora/dados/dominio.py)
- **Repositório:** [src/aurora/dados/repositorio.py](../../src/aurora/dados/repositorio.py)
- **Testes de schema:** [tests/integracao/test_persistencia.py](../../tests/integracao/test_persistencia.py)

---

**Última atualização:** 2026-10-07  
**Status:** ✅ Completo
