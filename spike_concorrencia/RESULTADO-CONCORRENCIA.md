# E10 — Spike de concorrência e idempotência

## Hipóteses e critérios, registrados antes da execução

Data do registro prévio: 2026-09-24.

### Hipóteses

- **E0 — SELECT seguido de INSERT, sem restrição:** será refutada como estratégia segura. A janela entre a consulta e a gravação permitirá duas linhas ativas para a mesma área/data.
- **E1 — índice `UNIQUE` parcial + `IntegrityError`:** será sustentada. A restrição no SQLite arbitrará a disputa no instante do `INSERT`; a violação será convertida em `{"status":"recusada"}` ou, para repetição do mesmo apartamento, `{"status":"ja_reservada","codigo":"..."}`.
- **E2 — `BEGIN IMMEDIATE` + consulta + INSERT:** será sustentada. O lock de escrita adquirido antes da consulta serializará os concorrentes; contenção transitória será tratada por retry limitado.
- **E3 — lock apenas em memória:** funcionará no mesmo processo (threads e `asyncio.to_thread`), mas será refutada entre processos porque cada processo possui seu próprio lock.
- **Geração de código:** uma sequência transacional persistida no mesmo banco produzirá códigos crescentes e não repetirá códigos semente ou cancelados. Não será usado sorteio.
- **`busy_timeout=0`:** produzirá contenção observável. E1/E2 devem ocultar essa contenção com retry limitado; qualquer `database is locked` que esgote os retries contará como falha, mesmo que a resposta pública continue normal.

### Critérios de sucesso e refutação

O critério de sucesso de uma estratégia é **zero falhas em todas as disputas de todos os modos e cenários**.

- **C1, N=300:** exatamente uma resposta `reservada`, uma `recusada`, uma linha ativa no banco e resposta recusada exatamente `{"status":"recusada"}`.
- **C2, N=300:** uma resposta `reservada`, uma `ja_reservada`, códigos iguais e exatamente uma linha ativa.
- **C3, N=300:** duas tentativas contra uma reserva semente são recusadas; a linha semente permanece única e nenhuma resposta contém código ou apartamento.
- **C4, N=100:** após a disputa, cancela-se a vencedora e o outro apartamento consegue reservar; o novo código não pertence ao conjunto de códigos já emitidos, inclusive o cancelado.
- **C5, N=300:** duas reservas concorrentes de áreas diferentes são aceitas e recebem códigos distintos.
- Modos: duas threads; dois processos `spawn` com timeout padrão e zero; dois subprocessos com timeout padrão e zero; duas corrotinas usando `asyncio.to_thread`.
- Uma disputa falha se qualquer asserção do cenário falhar ou se ocorrer erro de infraestrutura. Exceções não atravessam a fronteira de `gravar_reserva`.
- O experimento global passa se E1 e E2 tiverem zero falhas, E0 e E3 falharem em pelo menos um modo concorrente, o banco temporário for removido e as três rodadas forem estáveis quanto a essas conclusões.

## Ajuste da matriz (decisão do orquestrador)

Motivo: a 1ª sessão da E10 parou aos 36 min 23 s sem as 3 execuções integrais. A matriz pedida então (N=300 em todos os modos, com pausa e criação de processos) não cabia no orçamento de 25–40 min. O owner autorizou a retomada em 24/09/2026 com matriz reduzida nos modos caros.

N efetivo nesta retomada (sem pausa entre as duas requisições; `race_pause_seconds=0`):

| Classe | Modos | C1 | C2 | C3 | C4 | C5 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Barato | `threads-default`, `asyncio-default` | 300 | 300 | 300 | 300 | 300 |
| Caro | `spawn-default`, `spawn-zero`, `subprocess-default`, `subprocess-zero` | 60 | 60 | 60 | 40 | 60 |

C4 barato usa N=300 (não o N=100 do critério prévio) porque o orquestrador fixou N=300 por cenário nos modos baratos. C4 caro usa N=40. Os critérios acima permanecem como foram escritos; a tabela desta seção é o N medido.

Sonda prévia (não conta como rodada integral): E3 `spawn-default` C1 N=60 → 39 falhas / 0 infra, duas linhas ativas, `interprocess=true`, 1,571 s. A disputa entre processos existe sem pausa artificial; não foi necessário aumentar a contenção.

## Resultados

**Estado: 3 execuções integrais medidas; conclusões estáveis.** E1 e E2: 0 falhas em todos os modos e cenários nas 3 rodadas. E0 e E3: falham em modos concorrentes; E3 falha **entre processos** (`spawn-*` e `subprocess-*`) e tem 0 falhas em `threads-default` e `asyncio-default` (o lock em memória serializa o mesmo processo). Bancos temporários removidos (`cleanup_ok=true` nas 3). `experiment_status=PASS` e exit 0 nas 3.

Ambiente observado no `run_start`: Python 3.12.10, SQLite 3.49.1, `journal_mode=WAL`, `lock_retries=80`, `race_pause_seconds=0`.

| Rodada | Início | Fim | elapsed_seconds (script) | elapsed wrapper | exit | E0 falhas (interproc.) | E1 | E2 | E3 falhas (interproc.) | infra E1/E2 | cleanup |
| ---: | --- | --- | ---: | ---: | ---: | --- | ---: | ---: | --- | --- | --- |
| 1 | 2026-09-24T19:51:19-0300 | 2026-09-24T19:56:31-0300 | 311.506 | 311.670 | 0 | 1065 (419) | 0 | 0 | 442 (442) | 0/0 | true |
| 2 | 2026-09-24T19:56:31-0300 | 2026-09-24T20:01:42-0300 | 311.694 | 311.869 | 0 | 1041 (438) | 0 | 0 | 422 (422) | 0/0 | true |
| 3 | 2026-09-24T20:01:42-0300 | 2026-09-24T20:06:47-0300 | 304.223 | 304.440 | 0 | 1021 (415) | 0 | 0 | 419 (419) | 0/0 | true |

Soma das 3 matrizes (fonte: linhas `=== RODADA N EXIT` de `saida-bruta-3-rodadas.txt`): 311.670 + 311.869 + 304.440 = **927.979 s** (~15,47 min). Sessão wrapper: `SESSION_START 2026-09-24T19:51:19-0300` → `SESSION_END 2026-09-24T20:06:47-0300`, `exit_codes=[0, 0, 0]`.

### Tabela estratégia × modo × cenário × N × falhas

Colunas `F` e `I` são falhas e erros de infraestrutura nas rodadas 1/2/3. Fonte: 360 linhas `event=summary` em `spike_concorrencia/saida-bruta-3-rodadas.txt`.

| Estratégia | Modo | Cenário | N | Falhas R1/R2/R3 | Infra R1/R2/R3 |
| --- | --- | --- | ---: | --- | --- |
| E0 | threads-default | C1 | 300 | 75/44/53 | 0/0/0 |
| E0 | threads-default | C2 | 300 | 115/109/116 | 0/0/0 |
| E0 | threads-default | C3 | 300 | 0/0/0 | 0/0/0 |
| E0 | threads-default | C4 | 300 | 115/101/127 | 0/0/0 |
| E0 | threads-default | C5 | 300 | 0/0/0 | 0/0/0 |
| E1 | threads-default | C1 | 300 | 0/0/0 | 0/0/0 |
| E1 | threads-default | C2 | 300 | 0/0/0 | 0/0/0 |
| E1 | threads-default | C3 | 300 | 0/0/0 | 0/0/0 |
| E1 | threads-default | C4 | 300 | 0/0/0 | 0/0/0 |
| E1 | threads-default | C5 | 300 | 0/0/0 | 0/0/0 |
| E2 | threads-default | C1 | 300 | 0/0/0 | 0/0/0 |
| E2 | threads-default | C2 | 300 | 0/0/0 | 0/0/0 |
| E2 | threads-default | C3 | 300 | 0/0/0 | 0/0/0 |
| E2 | threads-default | C4 | 300 | 0/0/0 | 0/0/0 |
| E2 | threads-default | C5 | 300 | 0/0/0 | 0/0/0 |
| E3 | threads-default | C1 | 300 | 0/0/0 | 0/0/0 |
| E3 | threads-default | C2 | 300 | 0/0/0 | 0/0/0 |
| E3 | threads-default | C3 | 300 | 0/0/0 | 0/0/0 |
| E3 | threads-default | C4 | 300 | 0/0/0 | 0/0/0 |
| E3 | threads-default | C5 | 300 | 0/0/0 | 0/0/0 |
| E0 | spawn-default | C1 | 60 | 26/40/37 | 0/0/0 |
| E0 | spawn-default | C2 | 60 | 35/37/39 | 0/0/0 |
| E0 | spawn-default | C3 | 60 | 0/0/0 | 0/0/0 |
| E0 | spawn-default | C4 | 40 | 26/26/23 | 0/0/0 |
| E0 | spawn-default | C5 | 60 | 0/0/0 | 0/0/0 |
| E1 | spawn-default | C1 | 60 | 0/0/0 | 0/0/0 |
| E1 | spawn-default | C2 | 60 | 0/0/0 | 0/0/0 |
| E1 | spawn-default | C3 | 60 | 0/0/0 | 0/0/0 |
| E1 | spawn-default | C4 | 40 | 0/0/0 | 0/0/0 |
| E1 | spawn-default | C5 | 60 | 0/0/0 | 0/0/0 |
| E2 | spawn-default | C1 | 60 | 0/0/0 | 0/0/0 |
| E2 | spawn-default | C2 | 60 | 0/0/0 | 0/0/0 |
| E2 | spawn-default | C3 | 60 | 0/0/0 | 0/0/0 |
| E2 | spawn-default | C4 | 40 | 0/0/0 | 0/0/0 |
| E2 | spawn-default | C5 | 60 | 0/0/0 | 0/0/0 |
| E3 | spawn-default | C1 | 60 | 40/34/35 | 0/0/0 |
| E3 | spawn-default | C2 | 60 | 40/36/40 | 0/0/0 |
| E3 | spawn-default | C3 | 60 | 0/0/0 | 0/0/0 |
| E3 | spawn-default | C4 | 40 | 24/30/17 | 0/0/0 |
| E3 | spawn-default | C5 | 60 | 0/0/0 | 0/0/0 |
| E0 | spawn-zero | C1 | 60 | 40/40/34 | 5/4/1 |
| E0 | spawn-zero | C2 | 60 | 43/35/32 | 3/0/6 |
| E0 | spawn-zero | C3 | 60 | 3/6/7 | 3/6/7 |
| E0 | spawn-zero | C4 | 40 | 30/29/29 | 1/1/3 |
| E0 | spawn-zero | C5 | 60 | 5/5/2 | 5/5/2 |
| E1 | spawn-zero | C1 | 60 | 0/0/0 | 0/0/0 |
| E1 | spawn-zero | C2 | 60 | 0/0/0 | 0/0/0 |
| E1 | spawn-zero | C3 | 60 | 0/0/0 | 0/0/0 |
| E1 | spawn-zero | C4 | 40 | 0/0/0 | 0/0/0 |
| E1 | spawn-zero | C5 | 60 | 0/0/0 | 0/0/0 |
| E2 | spawn-zero | C1 | 60 | 0/0/0 | 0/0/0 |
| E2 | spawn-zero | C2 | 60 | 0/0/0 | 0/0/0 |
| E2 | spawn-zero | C3 | 60 | 0/0/0 | 0/0/0 |
| E2 | spawn-zero | C4 | 40 | 0/0/0 | 0/0/0 |
| E2 | spawn-zero | C5 | 60 | 0/0/0 | 0/0/0 |
| E3 | spawn-zero | C1 | 60 | 37/37/38 | 6/2/6 |
| E3 | spawn-zero | C2 | 60 | 42/43/42 | 5/6/7 |
| E3 | spawn-zero | C3 | 60 | 8/5/6 | 8/5/6 |
| E3 | spawn-zero | C4 | 40 | 30/18/24 | 6/5/5 |
| E3 | spawn-zero | C5 | 60 | 10/11/7 | 10/11/7 |
| E0 | subprocess-default | C1 | 60 | 33/34/40 | 0/0/0 |
| E0 | subprocess-default | C2 | 60 | 37/47/35 | 0/0/0 |
| E0 | subprocess-default | C3 | 60 | 0/0/0 | 0/0/0 |
| E0 | subprocess-default | C4 | 40 | 29/27/27 | 0/0/0 |
| E0 | subprocess-default | C5 | 60 | 0/0/0 | 0/0/0 |
| E1 | subprocess-default | C1 | 60 | 0/0/0 | 0/0/0 |
| E1 | subprocess-default | C2 | 60 | 0/0/0 | 0/0/0 |
| E1 | subprocess-default | C3 | 60 | 0/0/0 | 0/0/0 |
| E1 | subprocess-default | C4 | 40 | 0/0/0 | 0/0/0 |
| E1 | subprocess-default | C5 | 60 | 0/0/0 | 0/0/0 |
| E2 | subprocess-default | C1 | 60 | 0/0/0 | 0/0/0 |
| E2 | subprocess-default | C2 | 60 | 0/0/0 | 0/0/0 |
| E2 | subprocess-default | C3 | 60 | 0/0/0 | 0/0/0 |
| E2 | subprocess-default | C4 | 40 | 0/0/0 | 0/0/0 |
| E2 | subprocess-default | C5 | 60 | 0/0/0 | 0/0/0 |
| E3 | subprocess-default | C1 | 60 | 35/37/37 | 0/0/0 |
| E3 | subprocess-default | C2 | 60 | 38/35/37 | 0/0/0 |
| E3 | subprocess-default | C3 | 60 | 0/0/0 | 0/0/0 |
| E3 | subprocess-default | C4 | 40 | 28/22/22 | 0/0/0 |
| E3 | subprocess-default | C5 | 60 | 0/0/0 | 0/0/0 |
| E0 | subprocess-zero | C1 | 60 | 41/36/38 | 10/2/9 |
| E0 | subprocess-zero | C2 | 60 | 38/42/33 | 2/8/3 |
| E0 | subprocess-zero | C3 | 60 | 4/7/8 | 4/7/8 |
| E0 | subprocess-zero | C4 | 40 | 26/25/29 | 1/4/7 |
| E0 | subprocess-zero | C5 | 60 | 3/2/2 | 3/2/2 |
| E1 | subprocess-zero | C1 | 60 | 0/0/0 | 0/0/0 |
| E1 | subprocess-zero | C2 | 60 | 0/0/0 | 0/0/0 |
| E1 | subprocess-zero | C3 | 60 | 0/0/0 | 0/0/0 |
| E1 | subprocess-zero | C4 | 40 | 0/0/0 | 0/0/0 |
| E1 | subprocess-zero | C5 | 60 | 0/0/0 | 0/0/0 |
| E2 | subprocess-zero | C1 | 60 | 0/0/0 | 0/0/0 |
| E2 | subprocess-zero | C2 | 60 | 0/0/0 | 0/0/0 |
| E2 | subprocess-zero | C3 | 60 | 0/0/0 | 0/0/0 |
| E2 | subprocess-zero | C4 | 40 | 0/0/0 | 0/0/0 |
| E2 | subprocess-zero | C5 | 60 | 0/0/0 | 0/0/0 |
| E3 | subprocess-zero | C1 | 60 | 40/37/39 | 5/9/3 |
| E3 | subprocess-zero | C2 | 60 | 36/41/37 | 1/8/9 |
| E3 | subprocess-zero | C3 | 60 | 5/6/4 | 5/6/4 |
| E3 | subprocess-zero | C4 | 40 | 24/24/29 | 6/2/5 |
| E3 | subprocess-zero | C5 | 60 | 5/6/5 | 5/6/5 |
| E0 | asyncio-default | C1 | 300 | 100/103/109 | 0/0/0 |
| E0 | asyncio-default | C2 | 300 | 122/117/97 | 0/0/0 |
| E0 | asyncio-default | C3 | 300 | 0/0/0 | 0/0/0 |
| E0 | asyncio-default | C4 | 300 | 119/129/104 | 0/0/0 |
| E0 | asyncio-default | C5 | 300 | 0/0/0 | 0/0/0 |
| E1 | asyncio-default | C1 | 300 | 0/0/0 | 0/0/0 |
| E1 | asyncio-default | C2 | 300 | 0/0/0 | 0/0/0 |
| E1 | asyncio-default | C3 | 300 | 0/0/0 | 0/0/0 |
| E1 | asyncio-default | C4 | 300 | 0/0/0 | 0/0/0 |
| E1 | asyncio-default | C5 | 300 | 0/0/0 | 0/0/0 |
| E2 | asyncio-default | C1 | 300 | 0/0/0 | 0/0/0 |
| E2 | asyncio-default | C2 | 300 | 0/0/0 | 0/0/0 |
| E2 | asyncio-default | C3 | 300 | 0/0/0 | 0/0/0 |
| E2 | asyncio-default | C4 | 300 | 0/0/0 | 0/0/0 |
| E2 | asyncio-default | C5 | 300 | 0/0/0 | 0/0/0 |
| E3 | asyncio-default | C1 | 300 | 0/0/0 | 0/0/0 |
| E3 | asyncio-default | C2 | 300 | 0/0/0 | 0/0/0 |
| E3 | asyncio-default | C3 | 300 | 0/0/0 | 0/0/0 |
| E3 | asyncio-default | C4 | 300 | 0/0/0 | 0/0/0 |
| E3 | asyncio-default | C5 | 300 | 0/0/0 | 0/0/0 |

Leitura estável: E1 e E2 são 0/0/0 em todas as 120 células × 3 rodadas. E3 é 0/0/0 em threads e asyncio (mesmo processo) e falha em spawn/subprocess (entre processos). E0 falha em C1/C2/C4 em todos os modos; C3/C5 só falham em timeout zero, e nesses casos `F=I` (a recusa pública veio de lock esgotado, não de duas linhas ativas).

### Trecho real — disputa que passa (E1, C1, rodada 1)

Uma reserva, uma recusa `{"status":"recusada"}` (sem apartamento nem código de terceiro, AC-17), uma linha ativa.

```text
{"detail": {"database_active_rows": [{"apartamento": "102", "area": "salao-de-festas", "ativa": 1, "codigo": "RSV-4822", "data": "2101-05-16"}], "iteration": 1, "mode": "threads-default", "passed": true, "requests": [{"apartamento": "101", "area": "salao-de-festas", "data": "2101-05-16"}, {"apartamento": "102", "area": "salao-de-festas", "data": "2101-05-16"}], "responses": [{"infra_error": null, "result": {"status": "recusada"}}, {"infra_error": null, "result": {"codigo": "RSV-4822", "status": "reservada"}}], "scenario": "C1", "strategy": "E1"}, "event": "passing_dispute_complete"}
```

### Trecho real — disputa que falha (E0, C1, rodada 1)

Duas respostas `reservada`, duas linhas ativas na mesma área+data. Respostas normais, sem erro de servidor.

```text
{"detail": {"database_active_rows": [{"apartamento": "101", "area": "salao-de-festas", "ativa": 1, "codigo": "RSV-4822", "data": "2041-05-15"}, {"apartamento": "102", "area": "salao-de-festas", "ativa": 1, "codigo": "RSV-4823", "data": "2041-05-15"}], "iteration": 1, "mode": "threads-default", "passed": false, "requests": [{"apartamento": "101", "area": "salao-de-festas", "data": "2041-05-15"}, {"apartamento": "102", "area": "salao-de-festas", "data": "2041-05-15"}], "responses": [{"infra_error": null, "result": {"codigo": "RSV-4822", "status": "reservada"}}, {"infra_error": null, "result": {"codigo": "RSV-4823", "status": "reservada"}}], "scenario": "C1", "strategy": "E0"}, "event": "failing_dispute_e0"}
```

### Trecho real — disputa que falha (E3 entre processos, C1, rodada 1)

Mesmo padrão de E0, em `spawn-default` (`interprocess=true`). O lock em memória não atravessa processos.

```text
{"detail": {"database_active_rows": [{"apartamento": "102", "area": "churrasqueira", "ativa": 1, "codigo": "RSV-6623", "data": "2231-05-17"}, {"apartamento": "101", "area": "churrasqueira", "ativa": 1, "codigo": "RSV-6624", "data": "2231-05-17"}], "iteration": 2, "mode": "spawn-default", "passed": false, "requests": [{"apartamento": "101", "area": "churrasqueira", "data": "2231-05-17"}, {"apartamento": "102", "area": "churrasqueira", "data": "2231-05-17"}], "responses": [{"infra_error": null, "result": {"codigo": "RSV-6624", "status": "reservada"}}, {"infra_error": null, "result": {"codigo": "RSV-6623", "status": "reservada"}}], "scenario": "C1", "strategy": "E3"}, "event": "failing_dispute_e3", "interprocess": true}
```

### Saída das 3 execuções (totais e encerramento)

Arquivo bruto completo: `mba-ia-desafio-criacao-agente/spike_concorrencia/saida-bruta-3-rodadas.txt` (61.467 bytes, 390 linhas). Trechos de encerramento, copiados do arquivo:

```text
=== MATRIX cheap=C1-C5 N=300; expensive=C1/C2/C3/C5 N=60 C4 N=40; race_pause_seconds=0 ===
=== SESSION_START 2026-09-24T19:51:19-0300 ===
=== RODADA 1 START 2026-09-24T19:51:19-0300 ===
{"e0_interprocess_failures": 419, "e3_interprocess_failures": 442, "event": "strategy_totals", "expected_conclusion": true, "failures": {"E0": 1065, "E1": 0, "E2": 0, "E3": 442}, "infra_errors": {"E0": 37, "E1": 0, "E2": 0, "E3": 57}, "round": 1}
{"cleanup_ok": true, "cleanup_removed": ["r1-29876-b7c98181.sqlite3"], "elapsed_seconds": 311.506, "event": "run_end", "experiment_status": "PASS", "round": 1}
=== RODADA 1 EXIT 0 elapsed_seconds=311.670 2026-09-24T19:56:31-0300 ===
=== RODADA 2 START 2026-09-24T19:56:31-0300 ===
{"e0_interprocess_failures": 438, "e3_interprocess_failures": 422, "event": "strategy_totals", "expected_conclusion": true, "failures": {"E0": 1041, "E1": 0, "E2": 0, "E3": 422}, "infra_errors": {"E0": 39, "E1": 0, "E2": 0, "E3": 60}, "round": 2}
{"cleanup_ok": true, "cleanup_removed": ["r2-26060-57f7bf51.sqlite3"], "elapsed_seconds": 311.694, "event": "run_end", "experiment_status": "PASS", "round": 2}
=== RODADA 2 EXIT 0 elapsed_seconds=311.869 2026-09-24T20:01:42-0300 ===
=== RODADA 3 START 2026-09-24T20:01:42-0300 ===
{"e0_interprocess_failures": 415, "e3_interprocess_failures": 419, "event": "strategy_totals", "expected_conclusion": true, "failures": {"E0": 1021, "E1": 0, "E2": 0, "E3": 419}, "infra_errors": {"E0": 48, "E1": 0, "E2": 0, "E3": 57}, "round": 3}
{"cleanup_ok": true, "cleanup_removed": ["r3-12828-2c9b0409.sqlite3"], "elapsed_seconds": 304.223, "event": "run_end", "experiment_status": "PASS", "round": 3}
=== RODADA 3 EXIT 0 elapsed_seconds=304.440 2026-09-24T20:06:47-0300 ===
=== SESSION_END 2026-09-24T20:06:47-0300 exit_codes=[0, 0, 0] ===
```

Comando reproduzível (raiz `mba-ia-desafio-criacao-agente/`, Python do `.venv`, sem instalar pacote):

```powershell
.\.venv\Scripts\python.exe -u spike_concorrencia\spike_concorrencia.py --rodada 1
.\.venv\Scripts\python.exe -u spike_concorrencia\spike_concorrencia.py --rodada 2
.\.venv\Scripts\python.exe -u spike_concorrencia\spike_concorrencia.py --rodada 3
```

Nesta sessão as 3 foram encadeadas por `spike_concorrencia/_run_3_rodadas.py` (helper; apagar) gravando em `saida-bruta-3-rodadas.txt`. Wrapper: START 2026-09-24T19:51:19.3222709-03:00, END 2026-09-24T20:06:47.4140564-03:00, `WRAPPER_EXIT=0`.

## Riscos e recomendação

- SQLite permite um escritor por vez. WAL melhora leitores+escritor, mas não cria múltiplos escritores.
- `busy_timeout=0` torna a contenção imediatamente visível. Em E0/E3 isso aparece como `infra_errors` (C3/C5 em modos `-zero`: F=I). A fronteira pública ainda devolve `{"status":"recusada"}` (AC-24: resposta normal, sem erro de servidor), mas o teste conta o esgotamento de retry como falha. E1/E2 absorveram a mesma contenção: 0 infra nas 3 rodadas. Retry deve ser limitado, com backoff, e acompanhado de timeout adequado — não `busy_timeout=0` em produção.
- `threading.Lock` (E3) protege só um processo. Evidência: 0 falhas em threads/asyncio × 3; 419–442 falhas só em spawn/subprocess. Não sustenta a invariante com múltiplos workers.
- `BEGIN IMMEDIATE` (E2) serializa quando toda gravação passa por esse protocolo, mas não deixa invariante declarativa contra um caminho futuro que faça SELECT+INSERT sem o lock.
- O índice `UNIQUE` parcial (E1) expressa a regra no armazenamento (AC-26: exclusividade no instante da gravação). Em outro banco, sintaxe do índice parcial, isolamento, classificação de erro e política de retry precisam ser revalidados; não transportar o comportamento do SQLite ao pé da letra.
- A sequência `code_sequences` incrementa na mesma transação da reserva. O código só é publicado depois do commit; rollback desfaz o incremento. Códigos cancelados permanecem com `codigo UNIQUE`, portanto não são reutilizados (AC-20). C4 passou em E1/E2 nas 3 rodadas.
- Recusa de E1/E2 em C1 e C3 é exatamente `{"status":"recusada"}` — não vaza apartamento nem código de terceiro (AC-17).

**PROPOSTA para decisão do owner (§14), ainda não adotada e não implementada na aplicação:** usar E1 na E12/E14/E22 — índice `UNIQUE` parcial sobre `(area, data) WHERE ativa=1`, captura de `IntegrityError`, idempotência consultada pelo mesmo apartamento e retry limitado para lock transitório. Manter a sequência transacional no banco. E2 pode ser defesa adicional quando houver leitura-modificação-gravação, mas não substitui a restrição declarativa. E0 e E3 estão refutadas (E0 em qualquer modo concorrente; E3 entre processos). Esta proposta agora está condicionada às 3 rodadas integrais medidas acima, não mais à matriz incompleta da 1ª sessão.

## Artefatos gerados e o que ignorar/apagar

- `spike_concorrencia/spike_concorrencia.py` — script do experimento (ajustes desta retomada: amostras E0 e E3 separadas; `e3_interprocess_failures` no critério de PASS).
- `spike_concorrencia/RESULTADO-CONCORRENCIA.md` — este relatório (hipóteses/critérios da 1ª sessão preservados).
- `spike_concorrencia/saida-bruta-3-rodadas.txt` — saída bruta completa das 3 execuções integrais (cite este caminho).
- `spike_concorrencia/saida-bruta-parcial.txt` — focais da 1ª sessão; **ignorar/apagar** (não é evidência das 3 rodadas).
- `spike_concorrencia/_run_3_rodadas.py` — helper desta sessão para encadear as 3 rodadas; **apagar**.
- Bancos `r<rodada>-<pid>-<id>.sqlite3` e sidecars `-wal`/`-shm` são temporários e foram apagados pelo script (`cleanup_removed` nas 3 rodadas; nenhum `*.sqlite3*` permaneceu no diretório após `SESSION_END`).
- Não editar `.gitignore`.
