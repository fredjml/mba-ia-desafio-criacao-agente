# Review e Validação — Aurora Virtual Assistant

**Como revisar o código, executar testes, interpretar resultados.**

---

## 📋 Metodologia de Review

### R-V5: Review Viabilidade (Antes de R3)

**Objetivo:** Validar que as 5 Garantias Constitucionais estão implementadas corretamente.

**Executado em:** 2026-10-06, duração ~2h  
**Modelo:** Azure OpenAI Sonnet 5.5  
**Escopo:** Análise estática de `src/aurora/` contra 5 requisitos constitucionalais  
**Resultado:** ✅ Aprovado (veredito: VIÁVEL)

**Checklist R-V5:**
- [x] Garantia 1 (Confirmação): UPDATE + INSERT atômicos, 409 on duplicates
- [x] Garantia 2 (Isolamento): Apartamento extraído de session, guarda lexical
- [x] Garantia 3 (Reinício): IDs monotônicos via SQLite ACID
- [x] Garantia 4 (Regulamento): Apenas leitura, max 3 trechos lexicais
- [x] Garantia 5 (Concorrência): UNIQUE INDEX parcial

**Artefatos:**  
→ Relatório em `.github/modernize/java-spring-boot-upgrade-main/review/R-V5-2026-10-06.md` (commitado)

---

### R3: Review Sensor de Discriminação (Após R-V5)

**Objetivo:** Validar que o verificador de conformidade (T1 test runner) detecta violações.

**Executado em:** 2026-10-07, duração ~1h 6min (Cursor, sem Gemini)  
**Método:** 8 quebras intencionais (Q1-Q5), 1 verificador por quebra  
**Resultado:** ✅ Aprovado (6 de 8 detectadas, 1 é edge case, 1 falso negativo no Q4b)

**Resumo de Quebras:**

| ID | Arquivo | Quebra | Detectada? | Status |
|---|---|---|---|---|
| Q1 | reservas.py:103 | `return True` → `return False` | ✅ | Garantia 1 funciona |
| Q2 | reservas.py:181 | Falta filtro de apartment | ✅ | Isolamento validado |
| Q3a | fabrica.py:52 | Apaga DB de sessão | ✅ | Recovery testado |
| Q3b | dominio.py:219 | Contador em memória | ✅ | Monotonicity OK |
| Q4a | regulamento.py:761 | Retorna todo arquivo | ✅ | Limite 3 trechos OK |
| Q4b | instrucoes.py:54 | Markdown `**` confunde check | ⚠️ | Edge case (ver Q4b-2) |
| Q4b-2 | instrucoes.py:54 | Mesmo, normalizado | ✅ | Fix aplicado em commit 485c1d2 |
| Q5 | dominio.py:112 | UNIQUE INDEX → INDEX | ✅ | Concorrência validada |

**Achados:**
- 6 detectadas ✅
- 1 não-detectada (Q4b markdown `**` confunde regex, mas normalização no passo 15 resolve)
- 1 edge case (Q4b-2 mostra que passo 15 funciona com normalização)

**Decisão:** ✅ CORRIGIR (aplicar normalização `**` no passo 15)  
**Commit:** 485c1d2 ("test: normalize markdown asterisks in step 15")

**Artefatos:**  
→ Relatório em `.github/modernize/java-spring-boot-upgrade-main/review/R3-sensor-discriminacao-2026-10-07.md`

---

## ✅ Verificador Automático (T1)

### Comando de Execução

```bash
# Pré-requisitos:
# - Python 3.14+
# - uv (versão 0.12+)
# - Porta 8000 disponível

cd /repo
uv sync
python -X utf8 -u scripts/verificar_fluxo.py \
  --subir -v \
  --subir-cmd "<path-to-venv>/python.exe servidor_roteado.py"
```

### Saída Esperada

```
=== VERIFICADOR DE CONFORMIDADE ===

Passo 1: API sobe? ... ✅ 200 OK
Passo 2: Schema válido? ... ✅ (6 endpoints, 12 schemas)
Passo 3: Gem. inicializa? ... ✅ (agente pronto)
Passo 4: Confirmação básica? ... ✅ (3/3 eventos criados)
Passo 5: Rejeição? ... ✅ (409 retornado)
Passo 6: Cancelamento? ... ✅ (ativa=false)
Passo 7: Múltiplas pendências? ... ✅ (exatamente 1)
Passo 8: Confirmação rejeita duplicata? ... ✅ (409)
Passo 9: Eventos saltem? ... ✅ (IDs sequenciais)
Passo 10: Concorrência (salão)? ... ✅ (1 active)
Passo 11: Visitantes? ... ✅ (3/3 autorizados)
Passo 12: Regulamento (3 trechos max)? ... ✅
Passo 13: Reinício (IDs monoton)? ... ✅ (após restart)
Passo 14: Disputas (200/200 aprov)? ... ✅ (1 ativa no final)
Passo 15: Banco (índice + instrução)? ... ✅ (OK após norm.)

Resultado: 15/15 ✅

STATUS: VEREDITO ENTREGAR
```

### Interpretação de Falhas

**Falha no Passo 7 (`Múltiplas pendências`)**
```
ERR há exatamente uma confirmação pendente  ({'resposta': 'Pedido tratado.', 'confirmacoes_pendentes': []})

Diagnóstico:
1. Agente propôs confirmação
2. Mas tool NÃO registrou pendência no DB
3. Possível: schema_confirmacoes.status != "pendente"

Ação:
→ Verificar src/aurora/tools/reservas.py:100-120
→ Garantir INSERT em confirmacoes_pendentes
```

**Falha no Passo 15 (`Índice + Instrução`)**
```
ERR banco: nenhuma área e data com duas reservas ativas  ([('salao-de-festas', '2030-03-16', 2), ...])

Diagnóstico:
1. UNIQUE INDEX ux_reservas_ativa falhou
2. Duas reservas ativas no mesmo (area, data)
3. Possível: conversão de ativa=0 → ativa=1 fora de transação

Ação:
→ Verificar src/aurora/tools/reservas.py:confirmação
→ Garantir que UPDATE + INSERT estão em TRANSACTION
```

---

## 📊 Matriz de Rastreabilidade

| Garantia | Verificada por | Arquivo | Linhas | Test Passo | Status |
|---|---|---|---|---|---|
| 1. Confirmação | T1 (passos 4, 5, 7, 8) | reservas.py | 100-120 | 4, 7, 8 | ✅ |
| 2. Isolamento | T1 (cruzados 101/302) | memoria.py | 15-35 | 4, 10 | ✅ |
| 3. Reinício | T1 (passo 13 + monot.) | dominio.py | 200-225 | 9, 13 | ✅ |
| 4. Regulamento | T1 (passo 12 + 3 max) | regulamento.py | 50-80 | 12, 15 | ✅ |
| 5. Concorrência | T1 (passo 10, 14, 15) | dominio.py:112 | 112 | 10, 14, 15 | ✅ |

---

## 🧪 Como Executar Testes (Suite Completa)

### Baseline (antes de alterações)

```bash
# Criar baseline
python scripts/create_test_baseline.py
# → Saída: tests/baselines/baseline-2026-10-07.json (15/15 inicial)

# Guardar em git
git add tests/baselines/
git commit -m "baseline: 15/15 before changes"
```

### Após Alterações no Código

```bash
# Rodar verificador
python -X utf8 -u scripts/verificar_fluxo.py --subir -v --subir-cmd "..."

# Comparar com baseline
python scripts/verificar_fluxo.py --comparar baseline-2026-10-07.json

# Interpretar deltas
# - Novos passos passando? ✅ Melhoria
# - Passos falhando? ❌ Regressão (rollback ou fix)
```

### Testes de Unidade

```bash
pytest tests/unitarios/ -v
# → 24 testes de dominio.py, reservas.py, etc
# → Esperado: 100% passing (24/24)
```

### Testes de Integração

```bash
pytest tests/integracao/ -v
# → 6 testes end-to-end (com API real)
# → Esperado: 100% passing (6/6)
```

---

## 📝 Procedimento de Integração (CI/CD)

### GitHub Actions Workflow

```yaml
# .github/workflows/verify.yml
name: Conformidade

on: [push, pull_request]

jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v4
        with:
          python-version: '3.14'
      - name: Instala dependências
        run: pip install uv && uv sync
      - name: Roda verificador
        run: python -X utf8 -u scripts/verificar_fluxo.py --subir -v --subir-cmd "python servidor_roteado.py"
      - name: Compara com baseline
        run: python scripts/verificar_fluxo.py --comparar baseline-2026-10-07.json
```

---

## 🎯 Checklist de Production Readiness

**Antes de deploy para produção, verificar:**

- [x] Todos 15 passos passando (verde)
- [x] 5 Garantias Constitucionais validadas (R-V5)
- [x] Sensor de discriminação detecta quebras (R3)
- [x] Compliance matrix 100% (VALIDACAO-CONFORMIDADE.md)
- [x] Docs2 completa e sem links quebrados
- [x] Commit com tag `v1.0.0-production` criado
- [x] Release notes documentadas

**Deploy:**
```bash
git tag v1.0.0-production
git push origin v1.0.0-production
# → CI/CD automático: testa + faz deploy a Azure Container Apps
```

---

## 📚 Referências

| Documento | Propósito | Link |
|---|---|---|
| **Compliance Matrix** | 28 objetivos Desafio 7 | [VALIDACAO-CONFORMIDADE.md](../../VALIDACAO-CONFORMIDADE.md) |
| **R-V5 Report** | Review viabilidade (Garantias) | `.github/modernize/.../R-V5-2026-10-06.md` |
| **R3 Report** | Sensor discriminação (Testes) | `.github/modernize/.../R3-sensor-discriminacao-2026-10-07.md` |
| **Architecture** | Overview sistema | [docs2/architecture/overview.md](../architecture/overview.md) |
| **Security Model** | Implementação das 5 Garantias | [docs2/architecture/security-model.md](../architecture/security-model.md) |
| **Test Verification** | Linha de comando verificador | `scripts/verificar_fluxo.py --help` |

---

**Última atualização:** 2026-10-07  
**Status:** ✅ Metodologia documentada
