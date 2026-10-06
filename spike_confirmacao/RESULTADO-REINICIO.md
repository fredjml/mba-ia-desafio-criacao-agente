# E9-C — confirmação persistida com reinício real

## Hipóteses e critérios (registrados antes da execução)

| Cenário | Hipótese | Sucesso / refutação |
| --- | --- | --- |
| R0 — sem reinício | O serviço endurecido preserva o resultado da B3 | Sucesso: aprovação executa exatamente 1x em 40/40; qualquer falha refuta |
| R1 — reinício normal | Ler pela API pública o último timestamp gravado antes do primeiro append do novo objeto preserva a ordem entre processos | Sucesso: fase 1 termina normalmente e fase 2 nova aprova com exatamente 1 linha em 40/40; qualquer falha refuta |
| R2 — morte forçada | O commit por evento já persistido sobrevive a `Popen.kill()`/`TerminateProcess` | Sucesso: confirmação comprovadamente gravada antes do kill e aprovação nova produz exatamente 1 linha em 40/40; qualquer falha refuta |
| R3 — reenvio | O consumo da confirmação persiste e um terceiro processo ignora a mesma aprovação | Sucesso: arquivo continua com 1 linha em 20/20; qualquer falha refuta |
| R4 — negação | A negação retomada em processo novo não executa a tool | Sucesso: 0 linhas em 20/20; qualquer falha refuta |
| R5 — serviço original | Sem o ajuste, a perda de ordem reaparece após reinício | Controle: medir e reportar a taxa real de falha em N=40 |
| R6 — InMemory | Um novo processo não encontra a sessão | Controle: registrar erro real e 0 linhas de efeito |
| R7 — concorrência | Duas aprovações iguais em paralelo podem revelar semântica at-least-once | Observacional: registrar 1 ou 2 linhas e erros em N=20; não integra o gate |

O modelo é um `StubLlm` local. Não há chamada de modelo, rede ou credencial.
Cada tentativa usa banco SQLite novo. A tool registra cada execução real em
`spike_confirmacao/efeitos_<execução>.log`, contado entre processos.

O serviço usa somente APIs públicas: sobrescrita de
`DatabaseSessionService.append_event`, leitura por
`DatabaseSessionService.get_session(..., config=None)`, `Session.events` e
`Event.timestamp`. Nenhuma API interna/privada é usada. Um
`asyncio.Lock` por `session_id` cobre leitura inicial, ajuste e append.

**Estado desta seção:** escrita antes da primeira execução da matriz.

## Resultado

Ambiente observado: Windows, Python 3.12.10, `google-adk==2.9.2`, modelo
`StubLlm`, zero chamadas de modelo/rede. O comando final terminou em
2026-09-24T20:37:55.101661+00:00.

| Cenário | N | Falhas / observação | Tempo médio por ciclo | Conclusão |
| --- | ---: | ---: | ---: | --- |
| R0 | 40 | **0/40** | 0,269882 s | Passou |
| R1 | 40 | **0/40** | 4,422690 s | Passou; fase 1 terminou normalmente e fase 2 usou processo/objetos novos |
| R2 | 40 | **0/40** | 3,915816 s | Passou; `Popen.kill()` retornou 1 em Windows e a fase 2 nova executou 1x |
| R3 | 20 | **0/20** | 6,502328 s | Passou; terceiro processo manteve 1 linha |
| R4 | 20 | **0/20** | 3,938708 s | Passou; negação manteve 0 linhas |
| R5 | 40 | **24/40 (60%)** | 3,736858 s | Controle original falhou de forma silenciosa em 60% |
| R6 | 1 | perda esperada observada | 3,038754 s | Novo processo produziu `SessionNotFoundError`; 0 linhas |
| R7 | 20 | 1 linha em 20/20; 2 linhas em 0/20; erro em 20/20 | 0,325433 s | Observacional: uma aprovação executou e a outra recebeu `StaleSessionError` em todas as tentativas |

O critério do contorno foi atendido: R0, R1 e R2 tiveram 0/40 falhas; R3 e
R4 tiveram 0/20. Isso valida o estado **testado com stub em ambiente
controlado**, não produção.

## Diagnóstico e ajustes do executor

Houve duas interrupções antes da execução final, ambas preservadas:

1. `RESULTADO-BRUTO-REINICIO.txt`: o serviço usava `math.nextafter`. Embora
   estrito em memória, o incremento era menor que a resolução de microssegundos
   persistida pelo SQLite; os eventos empatavam no banco e o UUID voltava a
   decidir a ordem. R0 parou após duas falhas iguais. O serviço foi corrigido
   para o incremento de 2 µs já medido na B3; o teste e os critérios não foram
   alterados.
2. `RESULTADO-BRUTO-REINICIO-APOS-CORRECAO.txt`: R0 e R1 passaram em 0/40.
   R2 produziu exatamente 1 efeito e nenhum erro, mas o classificador exigia
   código zero também do processo terminado deliberadamente. Em Windows,
   `Popen.kill()`/`TerminateProcess` retornou 1. A asserção foi corrigida para
   exigir término não-zero da fase morta e zero das fases normais; o critério
   funcional não mudou.

Na execução final, a releitura mostra a ordem correta dos eventos críticos:
`adk_request_confirmation` com timestamp `1790281811.2541215` antes do
`function_response` da tool com `1790281811.2541234`. O novo objeto da fase 2
registrou `persisted_reads=1`.

## Trecho real completo de um ciclo que passa (R1.1)

```text
CHILD_BEGIN label=R1.1.phase1
COMMAND <REPO>\.venv\Scripts\python.exe -u <REPO>\spike_confirmacao\spike_com_reinicio.py --mode phase1 --state <REPO>\spike_confirmacao\estado_R1_1_8fa6c498ddde4ccaa6986a9fde0d6467.json
<REPO>\spike_confirmacao\spike_com_reinicio.py:228: UserWarning: [EXPERIMENTAL] ResumabilityConfig: This feature is experimental and may change or be removed in future versions without notice. It may introduce breaking changes at any time.
  resumability_config=ResumabilityConfig(is_resumable=True),
App "e9c_r1_1_1da08445cf77438d942136a99426589a" can transfer between agents but has no context_cache_config. Every transfer swaps the system instruction and the tool set, so the request prefix changes and the whole prompt is re-sent uncached after each transfer. Set context_cache_config on the app to give each agent its own cache.
<REPO>\.venv\Lib\site-packages\google\adk\tools\transfer_to_agent_tool.py:106: UserWarning: [EXPERIMENTAL] feature FeatureName.JSON_SCHEMA_FOR_FUNC_DECL is enabled.
  function_decl = super()._get_declaration()
Skipping missing token usage metadata for agent principal and model stub-local-principal
<REPO>\.venv\Lib\site-packages\google\adk\features\_feature_decorator.py:71: UserWarning: [EXPERIMENTAL] feature FeatureName.TOOL_CONFIRMATION is enabled.
  check_feature_enabled()
Skipping missing token usage metadata for agent especialista and model stub-local-especialista
SESSION label=phase1_pending status=found events=7
EVENT order=0 id=9f805632-1eba-46a2-83af-3316fcf49b98 author=user timestamp=1790281811.162625 invocation_id=e-6427d18a-f9c4-4220-a1e2-05d17fbb6439 calls=<none> responses=<none> requested=<none>
EVENT order=1 id=1943c924-b1ca-4d85-8feb-8b4bd81a3c2c author=principal timestamp=1790281811.2210808 invocation_id=e-6427d18a-f9c4-4220-a1e2-05d17fbb6439 calls=transfer_to_agent:fc-transfer responses=<none> requested=<none>
EVENT order=2 id=1e4cd477-08fb-4da9-baa0-ccd4df00c269 author=principal timestamp=1790281811.229606 invocation_id=e-6427d18a-f9c4-4220-a1e2-05d17fbb6439 calls=<none> responses=transfer_to_agent:fc-transfer requested=<none>
EVENT order=3 id=ccca2583-ed10-4cd8-a908-8df0f064c0e3 author=principal timestamp=1790281811.235604 invocation_id=e-6427d18a-f9c4-4220-a1e2-05d17fbb6439 calls=<none> responses=<none> requested=<none>
EVENT order=4 id=c4c58580-4106-4390-92a5-0b420bbbe2c2 author=especialista timestamp=1790281811.246117 invocation_id=e-6427d18a-f9c4-4220-a1e2-05d17fbb6439 calls=acao_confirmada:fc-acao responses=<none> requested=<none>
EVENT order=5 id=9539e8eb-cc4e-46ed-903e-aef8662819f3 author=especialista timestamp=1790281811.2541215 invocation_id=e-6427d18a-f9c4-4220-a1e2-05d17fbb6439 calls=adk_request_confirmation:adk-cca031f0-11f2-4bbf-a12c-36793b054d61 responses=<none> requested=<none>
EVENT order=6 id=db015a87-8e6e-4a51-a0ac-d84cab8ae1ab author=especialista timestamp=1790281811.2541234 invocation_id=e-6427d18a-f9c4-4220-a1e2-05d17fbb6439 calls=<none> responses=acao_confirmada:fc-acao requested=fc-acao
SERVICE label=phase1_pending adjustments=1 persisted_reads=1
PHASE1 status=pending_persisted error=none effects=0
COMMAND_EXIT code=0
CHILD_END label=R1.1.phase1
CHILD_BEGIN label=R1.1.phase2
COMMAND <REPO>\.venv\Scripts\python.exe -u <REPO>\spike_confirmacao\spike_com_reinicio.py --mode phase2 --state <REPO>\spike_confirmacao\estado_R1_1_8fa6c498ddde4ccaa6986a9fde0d6467.json --decision approve
<REPO>\spike_confirmacao\spike_com_reinicio.py:228: UserWarning: [EXPERIMENTAL] ResumabilityConfig: This feature is experimental and may change or be removed in future versions without notice. It may introduce breaking changes at any time.
  resumability_config=ResumabilityConfig(is_resumable=True),
App "e9c_r1_1_1da08445cf77438d942136a99426589a" can transfer between agents but has no context_cache_config. Every transfer swaps the system instruction and the tool set, so the request prefix changes and the whole prompt is re-sent uncached after each transfer. Set context_cache_config on the app to give each agent its own cache.
<REPO>\.venv\Lib\site-packages\google\adk\models\llm_request.py:298: UserWarning: [EXPERIMENTAL] feature FeatureName.JSON_SCHEMA_FOR_FUNC_DECL is enabled.
  declaration = tool._get_declaration()
Skipping missing token usage metadata for agent especialista and model stub-local-especialista
PHASE2 confirmed=True error=none events=3 effects=1
SESSION label=phase2_after_response status=found events=11
EVENT order=0 id=9f805632-1eba-46a2-83af-3316fcf49b98 author=user timestamp=1790281811.162625 invocation_id=e-6427d18a-f9c4-4220-a1e2-05d17fbb6439 calls=<none> responses=<none> requested=<none>
EVENT order=1 id=1943c924-b1ca-4d85-8feb-8b4bd81a3c2c author=principal timestamp=1790281811.2210808 invocation_id=e-6427d18a-f9c4-4220-a1e2-05d17fbb6439 calls=transfer_to_agent:fc-transfer responses=<none> requested=<none>
EVENT order=2 id=1e4cd477-08fb-4da9-baa0-ccd4df00c269 author=principal timestamp=1790281811.229606 invocation_id=e-6427d18a-f9c4-4220-a1e2-05d17fbb6439 calls=<none> responses=transfer_to_agent:fc-transfer requested=<none>
EVENT order=3 id=ccca2583-ed10-4cd8-a908-8df0f064c0e3 author=principal timestamp=1790281811.235604 invocation_id=e-6427d18a-f9c4-4220-a1e2-05d17fbb6439 calls=<none> responses=<none> requested=<none>
EVENT order=4 id=c4c58580-4106-4390-92a5-0b420bbbe2c2 author=especialista timestamp=1790281811.246117 invocation_id=e-6427d18a-f9c4-4220-a1e2-05d17fbb6439 calls=acao_confirmada:fc-acao responses=<none> requested=<none>
EVENT order=5 id=9539e8eb-cc4e-46ed-903e-aef8662819f3 author=especialista timestamp=1790281811.2541215 invocation_id=e-6427d18a-f9c4-4220-a1e2-05d17fbb6439 calls=adk_request_confirmation:adk-cca031f0-11f2-4bbf-a12c-36793b054d61 responses=<none> requested=<none>
EVENT order=6 id=db015a87-8e6e-4a51-a0ac-d84cab8ae1ab author=especialista timestamp=1790281811.2541234 invocation_id=e-6427d18a-f9c4-4220-a1e2-05d17fbb6439 calls=<none> responses=acao_confirmada:fc-acao requested=fc-acao
EVENT order=7 id=8bc584d1-1afc-4852-8a02-7c0f72d9671a author=user timestamp=1790281813.2882273 invocation_id=e-6427d18a-f9c4-4220-a1e2-05d17fbb6439 calls=<none> responses=adk_request_confirmation:adk-cca031f0-11f2-4bbf-a12c-36793b054d61 requested=<none>
EVENT order=8 id=083c0d2f-6fe4-4db9-9786-67e5b13bf8b8 author=especialista timestamp=1790281813.3385262 invocation_id=e-6427d18a-f9c4-4220-a1e2-05d17fbb6439 calls=<none> responses=acao_confirmada:fc-acao requested=<none>
EVENT order=9 id=2c0dba5c-262f-4ec2-8f1b-98f137c53071 author=especialista timestamp=1790281813.3625371 invocation_id=e-6427d18a-f9c4-4220-a1e2-05d17fbb6439 calls=<none> responses=<none> requested=<none>
EVENT order=10 id=e64f89e6-87f6-44c3-80e2-63d11c4b171c author=especialista timestamp=1790281813.3720298 invocation_id=e-6427d18a-f9c4-4220-a1e2-05d17fbb6439 calls=<none> responses=<none> requested=<none>
SERVICE label=phase2_after_response adjustments=0 persisted_reads=1
COMMAND_EXIT code=0
CHILD_END label=R1.1.phase2
TRIAL scenario=R1 index=1 status=PASS service=ordered killed=False decision=approve replay=False effects=1 errors=none;none process_codes=0,0 elapsed_seconds=4.073517
CLEANUP removed=spike_e9c_R1_1_8fa6c498ddde4ccaa6986a9fde0d6467.db,efeitos_R1_1_8fa6c498ddde4ccaa6986a9fde0d6467.log,estado_R1_1_8fa6c498ddde4ccaa6986a9fde0d6467.json
```

## Trecho real completo de um ciclo que falha (controle R5.1)

```text
CHILD_BEGIN label=R5.1.phase1
COMMAND <REPO>\.venv\Scripts\python.exe -u <REPO>\spike_confirmacao\spike_com_reinicio.py --mode phase1 --state <REPO>\spike_confirmacao\estado_R5_1_c680ba03847d49d29882ab6ae0290416.json
<REPO>\spike_confirmacao\spike_com_reinicio.py:228: UserWarning: [EXPERIMENTAL] ResumabilityConfig: This feature is experimental and may change or be removed in future versions without notice. It may introduce breaking changes at any time.
  resumability_config=ResumabilityConfig(is_resumable=True),
App "e9c_r5_1_9e53ee799fe44d0c81921ee125e26b15" can transfer between agents but has no context_cache_config. Every transfer swaps the system instruction and the tool set, so the request prefix changes and the whole prompt is re-sent uncached after each transfer. Set context_cache_config on the app to give each agent its own cache.
<REPO>\.venv\Lib\site-packages\google\adk\tools\transfer_to_agent_tool.py:106: UserWarning: [EXPERIMENTAL] feature FeatureName.JSON_SCHEMA_FOR_FUNC_DECL is enabled.
  function_decl = super()._get_declaration()
Skipping missing token usage metadata for agent principal and model stub-local-principal
<REPO>\.venv\Lib\site-packages\google\adk\features\_feature_decorator.py:71: UserWarning: [EXPERIMENTAL] feature FeatureName.TOOL_CONFIRMATION is enabled.
  check_feature_enabled()
Skipping missing token usage metadata for agent especialista and model stub-local-especialista
SESSION label=phase1_pending status=found events=7
EVENT order=0 id=561af7a7-6ec6-47b8-b20f-d178f3590789 author=user timestamp=1790282117.4410458 invocation_id=e-159094d9-d6ae-4751-8657-42c698d74852 calls=<none> responses=<none> requested=<none>
EVENT order=1 id=467951ce-269c-47d8-a4b3-58e210ff33e6 author=principal timestamp=1790282117.5080953 invocation_id=e-159094d9-d6ae-4751-8657-42c698d74852 calls=transfer_to_agent:fc-transfer responses=<none> requested=<none>
EVENT order=2 id=bec39f9f-5205-4d32-8ab9-ce8f74cc31d9 author=principal timestamp=1790282117.5180905 invocation_id=e-159094d9-d6ae-4751-8657-42c698d74852 calls=<none> responses=transfer_to_agent:fc-transfer requested=<none>
EVENT order=3 id=0808d911-54b5-447c-bf92-5e9e010d4c30 author=principal timestamp=1790282117.5250895 invocation_id=e-159094d9-d6ae-4751-8657-42c698d74852 calls=<none> responses=<none> requested=<none>
EVENT order=4 id=b8b1037a-2d2c-4c3f-b3ab-5e50df2b6ff9 author=especialista timestamp=1790282117.537091 invocation_id=e-159094d9-d6ae-4751-8657-42c698d74852 calls=acao_confirmada:fc-acao responses=<none> requested=<none>
EVENT order=5 id=0b5fc601-d0d5-4975-b8fe-22a4067eb7c5 author=especialista timestamp=1790282117.5458891 invocation_id=e-159094d9-d6ae-4751-8657-42c698d74852 calls=<none> responses=acao_confirmada:fc-acao requested=fc-acao
EVENT order=6 id=f7049974-773d-481b-be7e-1de402f6a170 author=especialista timestamp=1790282117.5464344 invocation_id=e-159094d9-d6ae-4751-8657-42c698d74852 calls=adk_request_confirmation:adk-3b23aab4-6937-4395-a449-ff82ceab6602 responses=<none> requested=<none>
SERVICE label=phase1_pending adjustments=0 persisted_reads=0
PHASE1 status=pending_persisted error=none effects=0
COMMAND_EXIT code=0
CHILD_END label=R5.1.phase1
CHILD_BEGIN label=R5.1.phase2
COMMAND <REPO>\.venv\Scripts\python.exe -u <REPO>\spike_confirmacao\spike_com_reinicio.py --mode phase2 --state <REPO>\spike_confirmacao\estado_R5_1_c680ba03847d49d29882ab6ae0290416.json --decision approve
<REPO>\spike_confirmacao\spike_com_reinicio.py:228: UserWarning: [EXPERIMENTAL] ResumabilityConfig: This feature is experimental and may change or be removed in future versions without notice. It may introduce breaking changes at any time.
  resumability_config=ResumabilityConfig(is_resumable=True),
App "e9c_r5_1_9e53ee799fe44d0c81921ee125e26b15" can transfer between agents but has no context_cache_config. Every transfer swaps the system instruction and the tool set, so the request prefix changes and the whole prompt is re-sent uncached after each transfer. Set context_cache_config on the app to give each agent its own cache.
PHASE2 confirmed=True error=none events=0 effects=0
SESSION label=phase2_after_response status=found events=8
EVENT order=0 id=561af7a7-6ec6-47b8-b20f-d178f3590789 author=user timestamp=1790282117.4410458 invocation_id=e-159094d9-d6ae-4751-8657-42c698d74852 calls=<none> responses=<none> requested=<none>
EVENT order=1 id=467951ce-269c-47d8-a4b3-58e210ff33e6 author=principal timestamp=1790282117.5080953 invocation_id=e-159094d9-d6ae-4751-8657-42c698d74852 calls=transfer_to_agent:fc-transfer responses=<none> requested=<none>
EVENT order=2 id=bec39f9f-5205-4d32-8ab9-ce8f74cc31d9 author=principal timestamp=1790282117.5180905 invocation_id=e-159094d9-d6ae-4751-8657-42c698d74852 calls=<none> responses=transfer_to_agent:fc-transfer requested=<none>
EVENT order=3 id=0808d911-54b5-447c-bf92-5e9e010d4c30 author=principal timestamp=1790282117.5250895 invocation_id=e-159094d9-d6ae-4751-8657-42c698d74852 calls=<none> responses=<none> requested=<none>
EVENT order=4 id=b8b1037a-2d2c-4c3f-b3ab-5e50df2b6ff9 author=especialista timestamp=1790282117.537091 invocation_id=e-159094d9-d6ae-4751-8657-42c698d74852 calls=acao_confirmada:fc-acao responses=<none> requested=<none>
EVENT order=5 id=0b5fc601-d0d5-4975-b8fe-22a4067eb7c5 author=especialista timestamp=1790282117.5458891 invocation_id=e-159094d9-d6ae-4751-8657-42c698d74852 calls=<none> responses=acao_confirmada:fc-acao requested=fc-acao
EVENT order=6 id=f7049974-773d-481b-be7e-1de402f6a170 author=especialista timestamp=1790282117.5464344 invocation_id=e-159094d9-d6ae-4751-8657-42c698d74852 calls=adk_request_confirmation:adk-3b23aab4-6937-4395-a449-ff82ceab6602 responses=<none> requested=<none>
EVENT order=7 id=a5eb32fa-7e02-4677-b960-3d11c1de724b author=user timestamp=1790282119.4912229 invocation_id=e-159094d9-d6ae-4751-8657-42c698d74852 calls=<none> responses=adk_request_confirmation:adk-3b23aab4-6937-4395-a449-ff82ceab6602 requested=<none>
SERVICE label=phase2_after_response adjustments=0 persisted_reads=0
COMMAND_EXIT code=0
CHILD_END label=R5.1.phase2
TRIAL scenario=R5 index=1 status=FAIL service=original killed=False decision=approve replay=False effects=0 errors=none;none process_codes=0,0 elapsed_seconds=4.013594
CLEANUP removed=spike_e9c_R5_1_c680ba03847d49d29882ab6ae0290416.db,estado_R5_1_c680ba03847d49d29882ab6ae0290416.json
```

## Riscos remanescentes

- **Escritor único:** o lock é apenas intraprocesso. Dois processos escrevendo a
  mesma sessão podem ler o mesmo último timestamp e produzir empate.
- **Relógio/clock skew:** regressão do relógio é mascarada por incrementos de
  2 µs; o timestamp deixa de representar exatamente o instante físico.
- **Concorrência entre processos:** não foi coordenada. R7 mede apenas duas
  corrotinas no mesmo processo e encontrou `StaleSessionError` em 20/20.
- **SQLite em produção:** este spike não prova disponibilidade, escalabilidade,
  contenção ou operação multiprocesso adequadas para produção.
- **Dependência de `append_event`:** o contorno depende de todos os eventos
  passarem pela sobrescrita pública e da ordenação interna observada no ADK.
- **Semântica at-least-once:** 0 duplicações em R7 não cria garantia
  exactly-once; a tool de aplicação continua obrigada a ser idempotente.
- **Manutenção:** a leitura e toda a matriz precisam ser repetidas quando o ADK
  mudar. Este spike não fixa uma versão futura.

## PROPOSTA de arquitetura de persistência para a E12/E13

**PROPOSTA para aprovação do owner (§14); não implementada nesta fatia e sem
fixar versão:**

1. Usar `App` com `ResumabilityConfig(is_resumable=True)`, tools protegidas com
   `require_confirmation=True` e um `SessionService` persistente equivalente ao
   `OrderedDatabaseSessionService`, condicionado à repetição desta matriz na
   versão de ADK que vier a ser escolhida.
2. Manter uma tabela própria de confirmações pendentes com ID único, sessão,
   tipo, payload imutável, status (`pending`, `approved`, `denied`), timestamps e
   chave idempotente da operação. A transição de `pending` deve ser condicional
   e transacional.
3. Fazer cada tool idempotente no armazenamento de domínio, com chave única da
   operação e restrições do banco. A tabela de efeitos deste spike conta
   invocações; ela não substitui essa garantia.
4. A rota de confirmação deve consultar/atualizar a pendência própria antes de
   chamar o Runner, devolver `409` para ID inexistente ou já respondido e
   traduzir o `ValueError` correspondente do ADK para `409`, sem efeito.
5. Não tratar SQLite como escolha de produção por causa deste resultado. A
   decisão do backend deve considerar transações, writer único e coordenação
   multiprocesso nas E12/E13.

## Reprodução e saídas completas

Comando executado a partir da raiz `mba-ia-desafio-criacao-agente/`:

```powershell
.\.venv\Scripts\python.exe spike_confirmacao\spike_com_reinicio.py
```

Saída bruta integral da execução final (9.155 linhas):

- `spike_confirmacao/RESULTADO-BRUTO-REINICIO-FINAL.txt`.

Saídas integrais das duas interrupções diagnosticadas:

- `spike_confirmacao/RESULTADO-BRUTO-REINICIO.txt`;
- `spike_confirmacao/RESULTADO-BRUTO-REINICIO-APOS-CORRECAO.txt`.

Resumo numérico real da execução final:

```text
SUMMARY scenario=R0 N=40 failures=0/40 average_seconds=0.269882
SUMMARY scenario=R1 N=40 failures=0/40 average_seconds=4.422690
SUMMARY scenario=R2 N=40 failures=0/40 average_seconds=3.915816
SUMMARY scenario=R3 N=20 failures=0/20 average_seconds=6.502328
SUMMARY scenario=R4 N=20 failures=0/20 average_seconds=3.938708
SUMMARY scenario=R5 N=40 failures=24/40 failure_rate_percent=60.00 average_seconds=3.736858
SUMMARY scenario=R6 N=1 expected_loss=PASS effects=0 errors=none;SessionNotFoundError:Session not found: session-d50d8891b24a40f4896b13d32433f2ac average_seconds=3.038754
SUMMARY scenario=R7 N=20 effects_1=20 effects_2=0 effects_other=0 trials_with_errors=20 average_seconds=0.325433
FINAL leftovers=<none>
SPIKE_COMPLETED utc=2026-09-24T20:37:55.101661+00:00 elapsed_seconds=698.456959 raw_output=RESULTADO-BRUTO-REINICIO-FINAL.txt
```
