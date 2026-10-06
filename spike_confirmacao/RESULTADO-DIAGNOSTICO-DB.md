# E9-B2 — Diagnóstico Database/SQLite + resumable

## Hipóteses e critérios de refutação

Esta tabela e os mesmos textos no docstring/script foram escritos antes da
primeira execução.

| Hipótese | Predição | Critério de refutação |
| --- | --- | --- |
| H1 — empate/ordem de timestamp | A leitura do SQLite muda a ordem dos eventos críticos e impede a primeira aprovação | Falhas sem anomalia de ordem, ou a mesma anomalia sem associação numérica com o resultado |
| H2 — dependência de tempo | A falha diminui com pausas de 0 s, 0,05 s, 0,25 s e 1 s | Taxas sem tendência conforme a pausa aumenta |
| H3 — estado do objeto | Runner e `DatabaseSessionService` novos reduzem a falha | Objetos novos não reduzem a taxa |
| H4 — `invocation_id`/ramo | Falhas não resolvem o ID ou não entram no caminho de retomada | Nas falhas, IDs coincidem e o ramo observado é `node_resumed` |
| Controle — InMemory | A primeira aprovação executa uma vez com N=20 por pausa | Falhas semelhantes às do SQLite |

Instrumentação: monkeypatch somente em memória de cinco métodos de `Runner`;
os wrappers apenas registram e delegam ao método original, sem mudar argumentos
ou retorno. Nenhum arquivo do ADK foi alterado.

## Ambiente e método

- Windows; Python do `.venv`; `google-adk==2.9.2`.
- Um processo, sem reinício; `StubLlm` local; nenhuma chamada de modelo/rede.
- Topologia `principal → especialista`; `FunctionTool(require_confirmation=True)`.
- Cada tentativa Database criou um `spike_diag_<uuid>.db` novo e o removeu.
- N=20 por configuração; 120 tentativas Database e 80 controles InMemory.

## Resultado numérico

| Hipótese/configuração | Taxa de falha | Conclusão |
| --- | ---: | --- |
| H1 — Database agregado | **72/120** | **Confirmada.** Ordem funcional incorreta, agente `principal` e falha coincidiram exatamente em 72/120; ordem correta, `especialista` e passe coincidiram em 48/120. |
| H2 — pausa 0 s | 10/20 (50%) | Sem tendência monotônica. |
| H2 — pausa 0,05 s | 14/20 (70%) | Sem tendência monotônica. |
| H2 — pausa 0,25 s | 11/20 (55%) | Sem tendência monotônica. |
| H2 — pausa 1 s | 9/20 (45%) | **H2 refutada:** pausa não elimina a falha. |
| H3(a) — mesmos Runner/serviço | 12/20 (60%) | Base da comparação. |
| H3(b) — Runner e serviço novos, mesmo DB | 16/20 (80%) | **H3 refutada:** objetos novos não resolvem nem reduzem a falha. |
| H4 — `invocation_id`/ramo | 0/120 IDs divergentes; 0/120 fora de `node_resumed` | ID e ramo estão corretos. A seleção do agente diverge: `principal` em 72/72 falhas e `especialista` em 48/48 passes. |
| Controle InMemory — 0 s | 0/20 | Passou. |
| Controle InMemory — 0,05 s | 0/20 | Passou. |
| Controle InMemory — 0,25 s | 0/20 | Passou. |
| Controle InMemory — 1 s | 0/20 | Controle agregado **0/80**. |

Associação de H1:

- todos os 48 passes Database tiveram empate entre os dois eventos críticos;
- 38/72 falhas também tiveram empate, mas o desempate por UUID colocou os
  eventos na ordem errada;
- nas outras 34/72 falhas, os timestamps eram distintos e a ordenação temporal
  necessariamente inverteu a ordem funcional;
- InMemory preservou a ordem funcional e passou 80/80, inclusive em 16/80
  casos nos quais os timestamps aparecem fora de ordem.

## Cadeia causal no ADK instalado

1. `flows/llm_flows/base_llm_flow.py:920-939` cria primeiro
   `function_response_event`, depois cria `tool_confirmation_event`, porém emite
   a confirmação antes da resposta (linhas 935-939).
2. `events/event.py:151-156` cria o timestamp no construtor. Logo, a confirmação
   emitida primeiro tem timestamp igual ou posterior ao response criado antes.
3. InMemory preserva a ordem de append: confirmação → response. Já
   `sessions/database_session_service.py:725-757` relê por
   `timestamp DESC, id DESC` e depois inverte a lista. Com timestamps distintos,
   resulta response → confirmação; em empate, UUID decide sem relação com a
   ordem de emissão.
4. `runners.py:1102-1145` carrega a sessão e escolhe `agent_to_run` antes de
   anexar a aprovação. `_agent_router.py:114-134` só retorna o autor da chamada
   quando o último evento é um FunctionResponse correspondente.
5. Na ordem correta, o último evento é o response de `acao_confirmada`, e o
   Runner escolhe `especialista`. Na ordem invertida, o último evento é
   `adk_request_confirmation`, e o Runner cai em `principal`.
   `request_confirmation.py:160-164` então ignora a chamada original, escrita
   por outro agente. Não há erro e a tool não executa.

## Trecho completo de caso que passa

```text
SESSION label=DB_DELAY_0_0.1.after_request app=diag_db_delay_0_0_6f76a11c45f94b32988e9a8dae52aef0 session=session-076d78c53691426d9ca026544452024e events=7
EVENT order=0 id=c1a420c9-22e6-4884-bcf5-7b73ad81d059 invocation_id=e-244da2e1-4544-4619-a477-56c8af63628a author=user timestamp=1790277256.5927532 function_calls=<none> function_responses=<none> requested_tool_confirmations=<none>
EVENT order=1 id=9d734b09-a83f-45bf-805b-27e509227b8a invocation_id=e-244da2e1-4544-4619-a477-56c8af63628a author=principal timestamp=1790277256.6586397 function_calls=transfer_to_agent:fc-transfer function_responses=<none> requested_tool_confirmations=<none>
EVENT order=2 id=113c71e1-ee5f-4767-91a7-e413a1fdba4d invocation_id=e-244da2e1-4544-4619-a477-56c8af63628a author=principal timestamp=1790277256.66834 function_calls=<none> function_responses=transfer_to_agent:fc-transfer requested_tool_confirmations=<none>
EVENT order=3 id=3cbd03da-bcf9-4b44-884e-09af48e41fb2 invocation_id=e-244da2e1-4544-4619-a477-56c8af63628a author=principal timestamp=1790277256.6739178 function_calls=<none> function_responses=<none> requested_tool_confirmations=<none>
EVENT order=4 id=99ba5811-1b4d-481f-96f6-ada8510fcc3a invocation_id=e-244da2e1-4544-4619-a477-56c8af63628a author=especialista timestamp=1790277256.683979 function_calls=acao_confirmada:fc-acao function_responses=<none> requested_tool_confirmations=<none>
EVENT order=5 id=0079aa21-40d3-4f03-b2d8-ac2c3b3d4e32 invocation_id=e-244da2e1-4544-4619-a477-56c8af63628a author=especialista timestamp=1790277256.7004356 function_calls=adk_request_confirmation:adk-3517251f-73f0-4700-a90f-db12ee6a2bd1 function_responses=<none> requested_tool_confirmations=<none>
EVENT order=6 id=2d7100cb-0d45-4968-9c21-5bb1df5f7661 invocation_id=e-244da2e1-4544-4619-a477-56c8af63628a author=especialista timestamp=1790277256.7004356 function_calls=<none> function_responses=acao_confirmada:fc-acao requested_tool_confirmations=fc-acao
ORDER label=DB_DELAY_0_0.1.after_request equal_adjacent=1 out_of_order_adjacent=0 tool_index=4 requested_index=6 confirmation_index=5 confirmation_order_ok=True
SESSION label=DB_DELAY_0_0.1.after_approval app=diag_db_delay_0_0_6f76a11c45f94b32988e9a8dae52aef0 session=session-076d78c53691426d9ca026544452024e events=11
EVENT order=0 id=c1a420c9-22e6-4884-bcf5-7b73ad81d059 invocation_id=e-244da2e1-4544-4619-a477-56c8af63628a author=user timestamp=1790277256.5927532 function_calls=<none> function_responses=<none> requested_tool_confirmations=<none>
EVENT order=1 id=9d734b09-a83f-45bf-805b-27e509227b8a invocation_id=e-244da2e1-4544-4619-a477-56c8af63628a author=principal timestamp=1790277256.6586397 function_calls=transfer_to_agent:fc-transfer function_responses=<none> requested_tool_confirmations=<none>
EVENT order=2 id=113c71e1-ee5f-4767-91a7-e413a1fdba4d invocation_id=e-244da2e1-4544-4619-a477-56c8af63628a author=principal timestamp=1790277256.66834 function_calls=<none> function_responses=transfer_to_agent:fc-transfer requested_tool_confirmations=<none>
EVENT order=3 id=3cbd03da-bcf9-4b44-884e-09af48e41fb2 invocation_id=e-244da2e1-4544-4619-a477-56c8af63628a author=principal timestamp=1790277256.6739178 function_calls=<none> function_responses=<none> requested_tool_confirmations=<none>
EVENT order=4 id=99ba5811-1b4d-481f-96f6-ada8510fcc3a invocation_id=e-244da2e1-4544-4619-a477-56c8af63628a author=especialista timestamp=1790277256.683979 function_calls=acao_confirmada:fc-acao function_responses=<none> requested_tool_confirmations=<none>
EVENT order=5 id=0079aa21-40d3-4f03-b2d8-ac2c3b3d4e32 invocation_id=e-244da2e1-4544-4619-a477-56c8af63628a author=especialista timestamp=1790277256.7004356 function_calls=adk_request_confirmation:adk-3517251f-73f0-4700-a90f-db12ee6a2bd1 function_responses=<none> requested_tool_confirmations=<none>
EVENT order=6 id=2d7100cb-0d45-4968-9c21-5bb1df5f7661 invocation_id=e-244da2e1-4544-4619-a477-56c8af63628a author=especialista timestamp=1790277256.7004356 function_calls=<none> function_responses=acao_confirmada:fc-acao requested_tool_confirmations=fc-acao
EVENT order=7 id=a47692f5-324b-4099-860e-c324d08aa271 invocation_id=e-244da2e1-4544-4619-a477-56c8af63628a author=user timestamp=1790277256.7222338 function_calls=<none> function_responses=adk_request_confirmation:adk-3517251f-73f0-4700-a90f-db12ee6a2bd1 requested_tool_confirmations=<none>
EVENT order=8 id=568e448d-31f5-4896-a03e-e8d85907fdda invocation_id=e-244da2e1-4544-4619-a477-56c8af63628a author=especialista timestamp=1790277256.7312026 function_calls=<none> function_responses=acao_confirmada:fc-acao requested_tool_confirmations=<none>
EVENT order=9 id=b7e20d3a-f9ea-4fe9-8989-7290ec9f9694 invocation_id=e-244da2e1-4544-4619-a477-56c8af63628a author=especialista timestamp=1790277256.7372115 function_calls=<none> function_responses=<none> requested_tool_confirmations=<none>
EVENT order=10 id=a79eee1d-af5d-410b-9997-dfdab37a6403 invocation_id=e-244da2e1-4544-4619-a477-56c8af63628a author=especialista timestamp=1790277256.745733 function_calls=<none> function_responses=<none> requested_tool_confirmations=<none>
ORDER label=DB_DELAY_0_0.1.after_approval equal_adjacent=1 out_of_order_adjacent=0 tool_index=4 requested_index=6 confirmation_index=5 confirmation_order_ok=True
TRIAL config=DB_DELAY_0_0 index=1 status=PASS pause=0.0 recreate=False counter=1 initial_error=none approval_error=none request_invocation_id=e-244da2e1-4544-4619-a477-56c8af63628a approval_invocation_id=e-244da2e1-4544-4619-a477-56c8af63628a invocation_match=True resumed_branch=True new_branch=False resumed_agent=especialista trace=[{"agent": "especialista", "branch": "node_runtime", "invocation_id": null, "stage": "find_agent"}, {"agent": null, "branch": "node_resumed", "invocation_id": "e-244da2e1-4544-4619-a477-56c8af63628a", "stage": "resolve_from_fr"}]
```

## Trecho completo de caso que falha

```text
SESSION label=DB_DELAY_0_0.2.after_request app=diag_db_delay_0_0_e6dcaeb7f45d4f018e5a422cd620a734 session=session-0de955f746d8475ebd2f85e527cc2277 events=7
EVENT order=0 id=6227957b-9e83-4a39-90e5-e3da9e03806e invocation_id=e-e0b30081-9fb5-4943-918b-4d132c9ccf00 author=user timestamp=1790277256.8435748 function_calls=<none> function_responses=<none> requested_tool_confirmations=<none>
EVENT order=1 id=78a34387-f2ee-4195-948c-5ec85170e451 invocation_id=e-e0b30081-9fb5-4943-918b-4d132c9ccf00 author=principal timestamp=1790277256.8532164 function_calls=transfer_to_agent:fc-transfer function_responses=<none> requested_tool_confirmations=<none>
EVENT order=2 id=cbd72cdb-1dfc-4df3-9c65-c6ca579495fe invocation_id=e-e0b30081-9fb5-4943-918b-4d132c9ccf00 author=principal timestamp=1790277256.8592074 function_calls=<none> function_responses=transfer_to_agent:fc-transfer requested_tool_confirmations=<none>
EVENT order=3 id=4f594f11-2e8d-4492-b22f-dd14abb32bb0 invocation_id=e-e0b30081-9fb5-4943-918b-4d132c9ccf00 author=principal timestamp=1790277256.865735 function_calls=<none> function_responses=<none> requested_tool_confirmations=<none>
EVENT order=4 id=dfc168b2-b519-4151-a21f-195eb2ccc569 invocation_id=e-e0b30081-9fb5-4943-918b-4d132c9ccf00 author=especialista timestamp=1790277256.8732173 function_calls=acao_confirmada:fc-acao function_responses=<none> requested_tool_confirmations=<none>
EVENT order=5 id=67514a51-bce5-43de-825c-16b63e23423e invocation_id=e-e0b30081-9fb5-4943-918b-4d132c9ccf00 author=especialista timestamp=1790277256.880437 function_calls=<none> function_responses=acao_confirmada:fc-acao requested_tool_confirmations=fc-acao
EVENT order=6 id=3564492b-25bb-4883-8f48-e62dc3abc6d9 invocation_id=e-e0b30081-9fb5-4943-918b-4d132c9ccf00 author=especialista timestamp=1790277256.880943 function_calls=adk_request_confirmation:adk-09dc7c4e-c151-47a5-bfed-61516c0dda8c function_responses=<none> requested_tool_confirmations=<none>
ORDER label=DB_DELAY_0_0.2.after_request equal_adjacent=0 out_of_order_adjacent=0 tool_index=4 requested_index=5 confirmation_index=6 confirmation_order_ok=False
SESSION label=DB_DELAY_0_0.2.after_approval app=diag_db_delay_0_0_e6dcaeb7f45d4f018e5a422cd620a734 session=session-0de955f746d8475ebd2f85e527cc2277 events=8
EVENT order=0 id=6227957b-9e83-4a39-90e5-e3da9e03806e invocation_id=e-e0b30081-9fb5-4943-918b-4d132c9ccf00 author=user timestamp=1790277256.8435748 function_calls=<none> function_responses=<none> requested_tool_confirmations=<none>
EVENT order=1 id=78a34387-f2ee-4195-948c-5ec85170e451 invocation_id=e-e0b30081-9fb5-4943-918b-4d132c9ccf00 author=principal timestamp=1790277256.8532164 function_calls=transfer_to_agent:fc-transfer function_responses=<none> requested_tool_confirmations=<none>
EVENT order=2 id=cbd72cdb-1dfc-4df3-9c65-c6ca579495fe invocation_id=e-e0b30081-9fb5-4943-918b-4d132c9ccf00 author=principal timestamp=1790277256.8592074 function_calls=<none> function_responses=transfer_to_agent:fc-transfer requested_tool_confirmations=<none>
EVENT order=3 id=4f594f11-2e8d-4492-b22f-dd14abb32bb0 invocation_id=e-e0b30081-9fb5-4943-918b-4d132c9ccf00 author=principal timestamp=1790277256.865735 function_calls=<none> function_responses=<none> requested_tool_confirmations=<none>
EVENT order=4 id=dfc168b2-b519-4151-a21f-195eb2ccc569 invocation_id=e-e0b30081-9fb5-4943-918b-4d132c9ccf00 author=especialista timestamp=1790277256.8732173 function_calls=acao_confirmada:fc-acao function_responses=<none> requested_tool_confirmations=<none>
EVENT order=5 id=67514a51-bce5-43de-825c-16b63e23423e invocation_id=e-e0b30081-9fb5-4943-918b-4d132c9ccf00 author=especialista timestamp=1790277256.880437 function_calls=<none> function_responses=acao_confirmada:fc-acao requested_tool_confirmations=fc-acao
EVENT order=6 id=3564492b-25bb-4883-8f48-e62dc3abc6d9 invocation_id=e-e0b30081-9fb5-4943-918b-4d132c9ccf00 author=especialista timestamp=1790277256.880943 function_calls=adk_request_confirmation:adk-09dc7c4e-c151-47a5-bfed-61516c0dda8c function_responses=<none> requested_tool_confirmations=<none>
EVENT order=7 id=7fe9c397-d8d3-40bf-8901-5e8b3b681bf8 invocation_id=e-e0b30081-9fb5-4943-918b-4d132c9ccf00 author=user timestamp=1790277256.9009652 function_calls=<none> function_responses=adk_request_confirmation:adk-09dc7c4e-c151-47a5-bfed-61516c0dda8c requested_tool_confirmations=<none>
ORDER label=DB_DELAY_0_0.2.after_approval equal_adjacent=0 out_of_order_adjacent=0 tool_index=4 requested_index=5 confirmation_index=6 confirmation_order_ok=False
TRIAL config=DB_DELAY_0_0 index=2 status=FAIL pause=0.0 recreate=False counter=0 initial_error=none approval_error=none request_invocation_id=e-e0b30081-9fb5-4943-918b-4d132c9ccf00 approval_invocation_id=e-e0b30081-9fb5-4943-918b-4d132c9ccf00 invocation_match=True resumed_branch=True new_branch=False resumed_agent=principal trace=[{"agent": "principal", "branch": "node_runtime", "invocation_id": null, "stage": "find_agent"}, {"agent": null, "branch": "node_resumed", "invocation_id": "e-e0b30081-9fb5-4943-918b-4d132c9ccf00", "stage": "resolve_from_fr"}]
```

## VEREDITO

**Causa PROVADA:** incompatibilidade entre a ordem de emissão dos dois eventos
de confirmação e a ordenação usada ao relê-los do banco. O ADK 2.9.2 cria o
response antes, mas emite `adk_request_confirmation` antes dele
(`base_llm_flow.py:920-939`). O DatabaseSessionService perde essa ordem ao reler
por `(timestamp, id)` (`database_session_service.py:725-757`). Isso muda o
agente escolhido (`_agent_router.py:114-134`): `especialista` em 48/48 passes e
`principal` em 72/72 falhas. No agente errado, a confirmação é ignorada
(`request_confirmation.py:160-164`).

H2, H3 e a parte de H4 relativa a `invocation_id`/ramo foram descartadas.

**PROPOSTA de contorno:** nenhuma proposta medida é aceitável. Pausas falharam
em 9–14/20 e Runner/serviço novos falharam em 16/20. Reenvio não foi medido
como configuração independente nesta fatia, portanto não é declarado contorno.
Uma correção de versão do ADK ou controle próprio de pendência/roteamento exige
novo experimento e está fora do escopo.

## Reprodução e saída real completa

```powershell
.\.venv\Scripts\python.exe spike_confirmacao\spike_diagnostico_db.py
```

Saída integral de `stdout` e `stderr`:
`spike_confirmacao/RESULTADO-BRUTO-DIAGNOSTICO-DB.txt` (5.154 linhas).

```text
SUMMARY config=DB_DELAY_0_0 failures=10/20 fail_equal_timestamp=6/10 pass_equal_timestamp=10/10 fail_out_of_order=0/10 pass_out_of_order=0/10 confirmation_bad_order=10/20 invocation_id_mismatch=0/20 not_resumed_branch=0/20 wrong_resumed_agent=10/20
SUMMARY config=DB_DELAY_0_05 failures=14/20 fail_equal_timestamp=10/14 pass_equal_timestamp=6/6 fail_out_of_order=0/14 pass_out_of_order=0/6 confirmation_bad_order=14/20 invocation_id_mismatch=0/20 not_resumed_branch=0/20 wrong_resumed_agent=14/20
SUMMARY config=DB_DELAY_0_25 failures=11/20 fail_equal_timestamp=4/11 pass_equal_timestamp=9/9 fail_out_of_order=0/11 pass_out_of_order=0/9 confirmation_bad_order=11/20 invocation_id_mismatch=0/20 not_resumed_branch=0/20 wrong_resumed_agent=11/20
SUMMARY config=DB_DELAY_1_0 failures=9/20 fail_equal_timestamp=5/9 pass_equal_timestamp=11/11 fail_out_of_order=0/9 pass_out_of_order=0/11 confirmation_bad_order=9/20 invocation_id_mismatch=0/20 not_resumed_branch=0/20 wrong_resumed_agent=9/20
SUMMARY config=DB_SAME_OBJECT failures=12/20 fail_equal_timestamp=5/12 pass_equal_timestamp=8/8 fail_out_of_order=0/12 pass_out_of_order=0/8 confirmation_bad_order=12/20 invocation_id_mismatch=0/20 not_resumed_branch=0/20 wrong_resumed_agent=12/20
SUMMARY config=DB_NEW_OBJECT failures=16/20 fail_equal_timestamp=8/16 pass_equal_timestamp=4/4 fail_out_of_order=0/16 pass_out_of_order=0/4 confirmation_bad_order=16/20 invocation_id_mismatch=0/20 not_resumed_branch=0/20 wrong_resumed_agent=16/20
SUMMARY config=MEMORY_DELAY_0_0 failures=0/20 fail_equal_timestamp=0/0 pass_equal_timestamp=20/20 fail_out_of_order=0/0 pass_out_of_order=4/20 confirmation_bad_order=0/20 invocation_id_mismatch=0/20 not_resumed_branch=0/20 wrong_resumed_agent=0/20
SUMMARY config=MEMORY_DELAY_0_05 failures=0/20 fail_equal_timestamp=0/0 pass_equal_timestamp=19/20 fail_out_of_order=0/0 pass_out_of_order=6/20 confirmation_bad_order=0/20 invocation_id_mismatch=0/20 not_resumed_branch=0/20 wrong_resumed_agent=0/20
SUMMARY config=MEMORY_DELAY_0_25 failures=0/20 fail_equal_timestamp=0/0 pass_equal_timestamp=20/20 fail_out_of_order=0/0 pass_out_of_order=3/20 confirmation_bad_order=0/20 invocation_id_mismatch=0/20 not_resumed_branch=0/20 wrong_resumed_agent=0/20
SUMMARY config=MEMORY_DELAY_1_0 failures=0/20 fail_equal_timestamp=0/0 pass_equal_timestamp=20/20 fail_out_of_order=0/0 pass_out_of_order=3/20 confirmation_bad_order=0/20 invocation_id_mismatch=0/20 not_resumed_branch=0/20 wrong_resumed_agent=0/20
CLEANUP database_leftovers=<none>
SPIKE_COMPLETED utc=2026-09-24T19:15:40.111865+00:00 elapsed_seconds=83.869797 raw_output=RESULTADO-BRUTO-DIAGNOSTICO-DB.txt
```
