# Peculiaridades e Limitações — Aurora Virtual Assistant

**Edge cases, comportamentos não-óbvios, limitações conhecidas.**

---

## ⚠️ ADK OrderedDatabaseSessionService

### Peculiaridade 1: Turno = Troca Completa de Contexto

```python
# Cada OrderedDatabaseSessionService.create_turn() é isolado
# Input: mensagem do usuário
# Output: todas as tool calls + resultados processados

# Implicação:
# - Não há "estado parcial" durante um turno
# - Se agente chamar 3 tools, todas completam antes de resposta final
# - Memory entre turnos = histórico (adk_turnos)

# Consequência para Aurora:
# Se user pede: "Reserve o salão E autorize meu visitante"
# Agente pode propor AMBAS confirmações no mesmo turno
# Mas cada confirmação é processada separadamente depois
```

### Peculiaridade 2: Recovery Pode Perder Último Turno

```python
# Se crash ocorrer DURANTE um turno (não finalizou):
# - INSERT user_message OK
# - UPDATE response NULL ← CRASH
# → Turno fica incompleto

# Recovery: OrderedDatabaseSessionService.skip_incomplete_turn()
# Próximo turno recomeça

# Implicação: User precisa redigitar mensagem
# Solução: Implementar cache de último user_message em RAM
```

### Peculiaridade 3: Session TTL é Manual

```python
# ADK não tem TTL automático
# Implementamos em script separado:

# src/aurora/manutencao/limpar_sessoes_expiradas.py
for session in Path("var").glob("*.db"):
    modified_time = session.stat().st_mtime
    age_days = (time.time() - modified_time) / 86400
    
    if age_days > 30:  # TTL = 30 dias
        session.unlink()

# Rodado via cron: 0 2 * * * python scripts/limpar_sessoes_expiradas.py
```

---

## ⚠️ SQLite Específico

### Peculiaridade 4: WAL Mode e Readers

```python
# SQLite WAL (Write-Ahead Logging) permite:
# - Múltiplos readers simultaneamente
# - 1 writer por vez
# NÃO permite: 2 writers paralelos

# Implicação:
# - 1000 users em paralelo = 1000 readers OK
# - Mas confirmações precisam serializar (fila de escritas)
# - Se 100 users confirmam reservas no mesmo segundo:
#   → ADK aguarda locks, não falha

# Edge case (raro):
# Writer aguarda >5s (timeout sqlite3.connect)
# → Timeout error, retorna 500 a user
```

### Peculiaridade 5: UNIQUE INDEX Parcial Tem Limitação

```sql
-- Criamos:
CREATE UNIQUE INDEX ux_reservas_ativa 
  ON reservas(area, data) WHERE ativa=1;

-- Garante: MAX 1 ativa por (area, data)
-- Mas NÃO garante em outras condições

-- Cenário perigoso:
-- Tabela tem: (area=salao, data=2030-03-10, ativa=1, id=RSV-1)
--             (area=salao, data=2030-03-10, ativa=0, id=RSV-2)  ← cancelada

-- UPDATE RSV-2 SET ativa=1
-- → Falha com: UNIQUE constraint failed
-- ✅ Correto! Impediu duplicação

-- UPDATE RSV-1 SET ativa=0
-- UPDATE RSV-2 SET ativa=1  (mas fora de transação)
-- → Sem crash, mas RACE CONDITION possível
```

**Mitigação:** Sempre usar `transaction()` context manager.

---

## ⚠️ Lexical Search (regulamento)

### Peculiaridade 6: Não Encontra Variações Ortográficas

```python
# Implementação:
regex = r"\b" + re.escape(keyword.lower()) + r"\b"

# Cenário 1: User pergunta "Qual é a taxa?"
# Busca por: "taxa"
# Encontra: "A taxa é cobrada..." ✅

# Cenário 2: User pergunta "E a tarifa?"
# Busca por: "tarifa"
# Regulamento tem: "taxa", "cobrança", "valor"
# Resultado: Não encontra ❌ (0 trechos)

# UX: Agente diz: "Não encontrei 'tarifa' no regulamento"
# Se quiser, tente 'taxa' ou 'cobrança'"

# Mitigação: Documentação do projeto instrui agente a usar sinonímia
```

### Peculiaridade 7: Pontuação Quebra Match

```python
# Regulamento tem: "Art. 1º — Cobrança..."
# User pergunta: "art 1"

# Regex: \bart\b
# Match falha em "Art." (ponto é token separator)

# Mitigação: Agente propõe "Art." se keyword=art não achar nada
```

### Peculiaridade 8: Retorna Máximo 3 Trechos (Limitado)

```python
# Se regulamento tem 20 parágrafos sobre "taxa":
# Retorna: top 3 por relevância (frequência de keyword)

# Implicação:
# Usuário pode não ver trechos mais relevantes
# (embora os 3 de maior frequência sejam bom proxy)

# Não há paginação / "ver mais resultados"
# Design: Agente resume os 3 + oferece consulta específica
```

---

## ⚠️ Isolamento de Apartamento

### Peculiaridade 9: Mudança de Contexto Quebra Isolamento

```python
# FastAPI recebe: X-Apartment: 101
# MemoriaAgente(session_context) lê contexto
# → apartamento locked como "101" neste turno

# Problema: Se header mudar entre turnos
# Turn 1: GET /chat (X-Apartment: 101) → agente vê "101"
# Turn 2: GET /chat (X-Apartment: 999) → NOVA sessão!

# Mitigation: OrderedDatabaseSessionService garante 1 session_id = 1 apartamento
# Implementação: session_id é bound ao apartamento no banco

# Mas se alguém:
# 1. Roubar session_id
# 2. Mudar header X-Apartment
# → Pode acessar outro apartamento!

# Mitigation final: Validar que X-Apartment == session.apartment
```

---

## ⚠️ Confirmação de Ação

### Peculiaridade 10: TTL Explícito de Confirmação

```python
# Criamos confirmação:
confirmacao_id = "conf-1"
timestamp = datetime.now()

# Confirmação expira em 5 minutos (PADRÃO)
# Se user demorar 6 min pra confirmar:
# POST /confirmations/conf-1 → 410 Gone

# Problema: User pode perder confirmação antiga
# Solução: Agente re-propõe confirmação

# Código:
expires_at = datetime.now() + timedelta(minutes=5)
if datetime.now() > expires_at:
    return {"error": "confirmation_expired", "codigo": 410}
```

### Peculiaridade 11: Confirmação Não é Reversível

```python
# Após confirmar:
# POST /confirmations/conf-1 {"confirmed": true}
# → Reserva criada, gravada no DB

# Não há "desconfirmar" dentro de um turno
# Se user muda de ideia APÓS confirmar:
# - Necessário novo turno: "Cancele a reserva X"
# - Agente oferece: "Quer cancelar RSV-4821?"
# - User confirma cancelamento

# Mitigação: UX clara = "Esta ação é irreversível"
```

---

## ⚠️ Crash e Recovery

### Peculiaridade 12: Retry Manual de Turno Incompleto

```python
# Cenário: Agente estava processando turno 5
# Crash ocorreu antes de COMMIT

# Recovery automática: adk_turnos.turn_number=5 fica incompleto
# ADK pula para turno 6

# Problema: User não sabe se ação foi executada
# "Eu pedi pra reservar o salão. Funcionou?"

# Mitigação:
# GET /events → Mostra últimos eventos
# "Nenhuma reserva criada" → Ação não foi executada
# User repete pedido

# Melhor: Implementar deduplicação de tool calls
# (idempotency key no user_message)
```

---

## 📊 Tabela: Peculiaridades por Severidade

| Peculiaridade | Severidade | Impacto | Mitigação |
|---|---|---|---|
| Turno = contexto completo | 🟡 MEDIUM | Confirmações multiplas / turno | Documentação |
| Recovery perde último turno | 🟡 MEDIUM | User retransmite | Cache em RAM |
| WAL timeout escritas paralelas | 🟡 MEDIUM | 500 error raro | Fila, retry exponencial |
| Léxical: variações ortográficas | 🟡 MEDIUM | UX: "tente outro termo" | Agente oferece sinonímia |
| Léxical: pontuação quebra match | 🟠 LOW | Raro se regulamento bem escrito | Agente tenta sem pontuação |
| TTL confirmação explícito | 🟠 LOW | User pode perder confirmação | Agente re-propõe |
| Confirmação não-reversível | 🟠 LOW | User precisa cancelar depois | UI clara = "irreversível" |
| Recovery manual | 🟡 MEDIUM | User incerto se ação funcionou | GET /events como source of truth |
| Isolamento: header bypass | 🔴 CRITICAL | Acesso cross-apartment | Validar X-Apartment == session.apartment |

---

## 🔍 Testes para Peculiaridades

```bash
# scripts/verificar_fluxo.py testa cada uma:

# Turno incompleto (crash)
# Passo 1-2: Agente sobe OK
# Passo 3: Simula crash via SIGKILL no meio de turno
# Passo 4: Recovery automática, próximo turno funciona

# Léxical (variações)
# Passo 12: Pergunta sobre "taxa" vs "tarifa"
# Verifica que agente sugere sinonímia

# Isolamento
# Passos cruzados (101 vs 302): Sem vazamento de dados

# Confirmação TTL
# Aguarda 6+ minutos, confirma → 410 Gone
```

---

**Última atualização:** 2026-10-07  
**Status:** ✅ Documentado
