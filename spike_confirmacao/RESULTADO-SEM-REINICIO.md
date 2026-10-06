# Resultado E9-B — confirmação sem reinício

## Ambiente e método

- Google ADK: `2.9.2`.
- Processo: único processo por execução completa da matriz; nenhum reinício dentro dos cenários.
- Modelo: `StubLlm`, implementação local de `BaseLlm`; nenhuma chamada de rede/modelo e nenhuma credencial.
- Topologia: `principal` transfere para `especialista`; o especialista tem `disallow_transfer_to_parent=True`, chama `acao_confirmada`, e essa `FunctionTool` usa `require_confirmation=True`.
- Serviços: `InMemorySessionService` e `DatabaseSessionService` com `sqlite+aiosqlite`, arquivo `spike_confirmacao/spike.db`.
- Comando, executado duas vezes:

```powershell
.\.venv\Scripts\python.exe spike_confirmacao\spike_sem_reinicio.py
```

As asserções de (1), (2) e (3) estavam no script antes da primeira execução. `PASS`/`FAIL` representa o comportamento esperado; `OBSERVED` registra comportamento bruto sem convertê-lo em contrato da futura API.

## Matriz observada

| ADK | SessionService | resumable | cenário | execução 1 | execução 2 | resultado |
| --- | --- | --- | --- | --- | --- | --- |
| 2.9.2 | InMemory | não | 1 — pedir confirmação sem executar | `PASS`, contador 0, autor `especialista` | igual | funciona |
| 2.9.2 | InMemory | não | 2 — aprovar executa uma vez | `FAIL`, contador 0, sem erro | igual | não funciona |
| 2.9.2 | InMemory | não | 3 — negar não executa | `PASS`, contador 0 | igual | funciona |
| 2.9.2 | InMemory | não | 4 — reenviar aprovação | ignorada; contador 0→0, sem erro | igual | ignorada, mas a primeira aprovação também não executou |
| 2.9.2 | InMemory | não | 5 — ID inexistente | `ValueError`, contador 0 | igual | erro explícito do ADK |
| 2.9.2 | InMemory | não | 6 — agente da retomada | autor `especialista`; nenhum modelo retomado; nenhuma execução | igual | não retoma o especialista |
| 2.9.2 | InMemory | sim | 1 — pedir confirmação sem executar | `PASS`, contador 0, autor `especialista` | igual | funciona |
| 2.9.2 | InMemory | sim | 2 — aprovar executa uma vez | `PASS`, contador 1 | igual | funciona |
| 2.9.2 | InMemory | sim | 3 — negar não executa | `PASS`, contador 0 | igual | funciona |
| 2.9.2 | InMemory | sim | 4 — reenviar aprovação | ignorada; contador 1→1, sem erro | igual | não reexecuta |
| 2.9.2 | InMemory | sim | 5 — ID inexistente | `ValueError`, contador 1 | igual | erro explícito do ADK |
| 2.9.2 | InMemory | sim | 6 — agente da retomada | autor, modelo retomado e executor da tool: `especialista` | igual | funciona |
| 2.9.2 | Database/SQLite | não | 1 — pedir confirmação sem executar | `PASS`, contador 0, autor `especialista` | igual | funciona |
| 2.9.2 | Database/SQLite | não | 2 — aprovar executa uma vez | `FAIL`, contador 0, sem erro | igual | não funciona |
| 2.9.2 | Database/SQLite | não | 3 — negar não executa | `PASS`, contador 0 | igual | funciona |
| 2.9.2 | Database/SQLite | não | 4 — reenviar aprovação | ignorada; contador 0→0, sem erro | igual | ignorada, mas a primeira aprovação também não executou |
| 2.9.2 | Database/SQLite | não | 5 — ID inexistente | `ValueError`, contador 0 | igual | erro explícito do ADK |
| 2.9.2 | Database/SQLite | não | 6 — agente da retomada | autor `especialista`; nenhum modelo retomado; nenhuma execução | igual | não retoma o especialista |
| 2.9.2 | Database/SQLite | sim | 1 — pedir confirmação sem executar | `PASS`, contador 0, autor `especialista` | igual | funciona |
| 2.9.2 | Database/SQLite | sim | 2 — aprovar executa uma vez | `FAIL`, contador 0, sem erro | `PASS`, contador 1 | **variou** |
| 2.9.2 | Database/SQLite | sim | 3 — negar não executa | `PASS`, contador 0 | igual | funciona |
| 2.9.2 | Database/SQLite | sim | 4 — reenviar aprovação | contador 0→1: o reenvio causou a primeira execução | contador 1→1: ignorado | **variou** |
| 2.9.2 | Database/SQLite | sim | 5 — ID inexistente | `ValueError`, contador 1 | igual | erro explícito do ADK |
| 2.9.2 | Database/SQLite | sim | 6 — agente da retomada | primeira aprovação: nenhum modelo retomado; a tool executou como `especialista` somente no reenvio | modelo e executor `especialista` na primeira aprovação | **variou** |

## Saída real que sustenta a matriz

### Execução 1 — linhas `RESULT`

```text
RESULT service=memory resumable=no scenario=1 status=PASS counter=0 confirmation_author=especialista error=none
RESULT service=memory resumable=no scenario=2 status=FAIL counter=0 error=none
RESULT service=memory resumable=no scenario=4 status=OBSERVED counter_before=0 counter_after=0 delta=0 error=none
RESULT service=memory resumable=no scenario=5 status=OBSERVED counter=0 error=ValueError:Function call not found for function response ids: {'id-inexistente'}. Ensure each function response ID matches an existing function call in the session history.
RESULT service=memory resumable=no scenario=6 status=OBSERVED confirmation_author=especialista resumed_models=<none> tool_execution_agents=<none>
RESULT service=memory resumable=no scenario=3 status=PASS counter=0 error=none
RESULT service=memory resumable=yes scenario=1 status=PASS counter=0 confirmation_author=especialista error=none
RESULT service=memory resumable=yes scenario=2 status=PASS counter=1 error=none
RESULT service=memory resumable=yes scenario=4 status=OBSERVED counter_before=1 counter_after=1 delta=0 error=none
RESULT service=memory resumable=yes scenario=5 status=OBSERVED counter=1 error=ValueError:Function call not found for function response ids: {'id-inexistente'}. Ensure each function response ID matches an existing function call in the session history.
RESULT service=memory resumable=yes scenario=6 status=OBSERVED confirmation_author=especialista resumed_models=especialista tool_execution_agents=especialista
RESULT service=memory resumable=yes scenario=3 status=PASS counter=0 error=none
RESULT service=database resumable=no scenario=1 status=PASS counter=0 confirmation_author=especialista error=none
RESULT service=database resumable=no scenario=2 status=FAIL counter=0 error=none
RESULT service=database resumable=no scenario=4 status=OBSERVED counter_before=0 counter_after=0 delta=0 error=none
RESULT service=database resumable=no scenario=5 status=OBSERVED counter=0 error=ValueError:Function call not found for function response ids: {'id-inexistente'}. Ensure each function response ID matches an existing function call in the session history.
RESULT service=database resumable=no scenario=6 status=OBSERVED confirmation_author=especialista resumed_models=<none> tool_execution_agents=<none>
RESULT service=database resumable=no scenario=3 status=PASS counter=0 error=none
RESULT service=database resumable=yes scenario=1 status=PASS counter=0 confirmation_author=especialista error=none
RESULT service=database resumable=yes scenario=2 status=FAIL counter=0 error=none
RESULT service=database resumable=yes scenario=4 status=OBSERVED counter_before=0 counter_after=1 delta=1 error=none
RESULT service=database resumable=yes scenario=5 status=OBSERVED counter=1 error=ValueError:Function call not found for function response ids: {'id-inexistente'}. Ensure each function response ID matches an existing function call in the session history.
RESULT service=database resumable=yes scenario=6 status=OBSERVED confirmation_author=especialista resumed_models=<none> tool_execution_agents=especialista
RESULT service=database resumable=yes scenario=3 status=PASS counter=0 error=none
```

### Execução 2 — linhas `RESULT`

```text
RESULT service=memory resumable=no scenario=1 status=PASS counter=0 confirmation_author=especialista error=none
RESULT service=memory resumable=no scenario=2 status=FAIL counter=0 error=none
RESULT service=memory resumable=no scenario=4 status=OBSERVED counter_before=0 counter_after=0 delta=0 error=none
RESULT service=memory resumable=no scenario=5 status=OBSERVED counter=0 error=ValueError:Function call not found for function response ids: {'id-inexistente'}. Ensure each function response ID matches an existing function call in the session history.
RESULT service=memory resumable=no scenario=6 status=OBSERVED confirmation_author=especialista resumed_models=<none> tool_execution_agents=<none>
RESULT service=memory resumable=no scenario=3 status=PASS counter=0 error=none
RESULT service=memory resumable=yes scenario=1 status=PASS counter=0 confirmation_author=especialista error=none
RESULT service=memory resumable=yes scenario=2 status=PASS counter=1 error=none
RESULT service=memory resumable=yes scenario=4 status=OBSERVED counter_before=1 counter_after=1 delta=0 error=none
RESULT service=memory resumable=yes scenario=5 status=OBSERVED counter=1 error=ValueError:Function call not found for function response ids: {'id-inexistente'}. Ensure each function response ID matches an existing function call in the session history.
RESULT service=memory resumable=yes scenario=6 status=OBSERVED confirmation_author=especialista resumed_models=especialista tool_execution_agents=especialista
RESULT service=memory resumable=yes scenario=3 status=PASS counter=0 error=none
RESULT service=database resumable=no scenario=1 status=PASS counter=0 confirmation_author=especialista error=none
RESULT service=database resumable=no scenario=2 status=FAIL counter=0 error=none
RESULT service=database resumable=no scenario=4 status=OBSERVED counter_before=0 counter_after=0 delta=0 error=none
RESULT service=database resumable=no scenario=5 status=OBSERVED counter=0 error=ValueError:Function call not found for function response ids: {'id-inexistente'}. Ensure each function response ID matches an existing function call in the session history.
RESULT service=database resumable=no scenario=6 status=OBSERVED confirmation_author=especialista resumed_models=<none> tool_execution_agents=<none>
RESULT service=database resumable=no scenario=3 status=PASS counter=0 error=none
RESULT service=database resumable=yes scenario=1 status=PASS counter=0 confirmation_author=especialista error=none
RESULT service=database resumable=yes scenario=2 status=PASS counter=1 error=none
RESULT service=database resumable=yes scenario=4 status=OBSERVED counter_before=1 counter_after=1 delta=0 error=none
RESULT service=database resumable=yes scenario=5 status=OBSERVED counter=1 error=ValueError:Function call not found for function response ids: {'id-inexistente'}. Ensure each function response ID matches an existing function call in the session history.
RESULT service=database resumable=yes scenario=6 status=OBSERVED confirmation_author=especialista resumed_models=especialista tool_execution_agents=especialista
RESULT service=database resumable=yes scenario=3 status=PASS counter=0 error=none
```

## Conclusões limitadas à fatia B

1. Sem resumability, os dois SessionServices emitem a confirmação, mas a aprovação retorna sem erro e não executa a tool do especialista. Essa é a falha silenciosa prevista por F3.
2. Com resumability, InMemory executou a aprovação exatamente uma vez nas duas execuções, ignorou o reenvio e retomou o `especialista`.
3. Database/SQLite com resumability não foi estável entre as duas execuções: uma atrasou a execução até o reenvio; a outra executou na primeira aprovação. Portanto, a combinação não pode ser declarada confiável pela fatia B.
4. ID inexistente produz `ValueError` bruto do ADK em todas as combinações. A futura API ainda precisa traduzir isso para `409`; esta fatia não testa HTTP.
5. Negação manteve contador zero nas quatro combinações.

## Hipóteses que continuam abertas para a fatia C

- comportamento depois de encerrar e iniciar outro processo;
- persistência e retomada da confirmação pendente no SQLite após reinício;
- causa da variação observada em Database/SQLite + resumability;
- se a primeira aprovação após reinício executa, atrasa até um reenvio ou é perdida;
- garantia exactly-once da camada de aplicação, que o ADK declara apenas como retomada at-least-once.

## Artefato SQLite

`spike_confirmacao/spike.db` é um artefato mutável gerado pelo teste. Ele deve ficar fora do Git. Esta fatia não altera `.gitignore`; basta não adicioná-lo a um commit. Qualquer política permanente de ignore exige autorização de outra fatia.
