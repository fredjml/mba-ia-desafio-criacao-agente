# E11 — Spike: tipo de agente/especialista compatível com resume

## Escopo e ambiente observado

- Data da execução: 2026-09-24.
- Windows 10.0.26100; Python 3.12.10; `google-adk==2.9.2`.
- `StubLlm` determinístico; `network_model_calls=0`; nenhuma credencial, `.env`
  ou variável de ambiente foi lida.
- SQLite novo por tentativa, sempre dentro de `spike_resume/`; todos os bancos,
  estados e arquivos de efeitos temporários foram removidos pelo próprio
  harness ao final. `FINAL leftovers=<none>`.
- Serviço de sessão: cópia autocontida do
  `spike_confirmacao/spike_servico_ordenado.py` da E9, com a origem declarada
  no cabeçalho.
- Estado probatório: testado com stub em ambiente controlado; não prova
  produção nem outra versão do ADK.

## Hipóteses e critérios — escritos antes da primeira execução

O texto abaixo já estava no docstring do harness e foi impresso nas linhas
2–22 da saída bruta antes do primeiro subprocesso:

| Hipótese | Hipótese prévia | Sucesso / refutação |
| --- | --- | --- |
| H1 | `LlmAgent` principal com dois especialistas `LlmAgent` e `App` resumable retoma a confirmação no especialista autor em processo novo | Para **cada** especialista: N=20, 0 falhas na aprovação com exatamente 1 efeito, reenvio mantendo 1, negação mantendo 0; qualquer diferença, erro ou autor/executor incorreto refuta |
| H2 | Texto novo não confirma nem executa a tool pendente e a confirmação original continua utilizável | Observacional, N=10: registrar efeito após texto, autores, erros e quantas confirmações originais ainda aprovam exatamente uma vez |
| H3 | Custom Agent mínimo retoma entre dois passos se persistir checkpoint explícito em `agent_state` | N=20 após `Popen.kill`: passo 1 não repete (1 linha), passo 2 executa (1 linha), sem erro; qualquer diferença/erro refuta |
| H4 | A decisão deve favorecer o tipo com melhor suporte medido a resume, confirmação, transferência e menor dependência de API interna/experimental | Comparação explícita e decisão apenas como PROPOSTA |

Duas falhas iguais sem hipótese nova encerrariam a matriz. Isso não ocorreu.

## Resultado numérico

| Hipótese / cenário | N | Falhas / observação | Conclusão |
| --- | ---: | ---: | --- |
| H1 — `especialista_reservas`, aprovar + reenviar | 20 | **0/20** | Passou: fase 2 deixou exatamente 1 efeito e o terceiro processo manteve 1 |
| H1 — `especialista_reservas`, negar | 20 | **0/20** | Passou: 0 efeitos |
| H1 — `especialista_visitantes`, aprovar + reenviar | 20 | **0/20** | Passou: fase 2 deixou exatamente 1 efeito e o terceiro processo manteve 1 |
| H1 — `especialista_visitantes`, negar | 20 | **0/20** | Passou: 0 efeitos |
| H2 — texto novo com confirmação pendente | 10 | 0/10 execuções acidentais; **0/10 pendências continuaram utilizáveis**; 0/10 erros | A primeira parte passou, mas a hipótese de que a pendência continuaria utilizável foi refutada em 10/10 |
| H3 — Custom Agent, morte entre passos | 20 | **0/20** | Passou: passo 1 = 1 e passo 2 = 1 em todas as tentativas |

Resumos integrais emitidos pelo harness:

```text
SUMMARY scenario=H1 specialist=especialista_reservas decision=approve N=20 failures=0/20 average_seconds=7.686086
SUMMARY scenario=H1 specialist=especialista_reservas decision=deny N=20 failures=0/20 average_seconds=5.475673
SUMMARY scenario=H1 specialist=especialista_visitantes decision=approve N=20 failures=0/20 average_seconds=8.184596
SUMMARY scenario=H1 specialist=especialista_visitantes decision=deny N=20 failures=0/20 average_seconds=5.168811
SUMMARY scenario=H2 N=10 pending_usable=0/10 accidental_execution=0/10 trials_with_errors=0/10 author_patterns={'especialista_reservas,especialista_reservas': 5, 'especialista_visitantes,especialista_visitantes': 5} average_seconds=7.141367
SUMMARY scenario=H3 N=20 failures=0/20 custom_resume_support_loc=11 average_seconds=4.056403
FINAL leftovers=<none>
SPIKE_COMPLETED utc=2026-09-24T21:21:08.256800+00:00 elapsed_seconds=683.013564 raw_output=RESULTADO-BRUTO-RESUME.txt
```

## H1 — agente que recebeu a retomada e executou a tool

Em 20/20 tentativas de aprovação de cada especialista:

- o evento `adk_request_confirmation` teve como `author` o especialista
  selecionado;
- os eventos da fase 2 foram todos do mesmo especialista;
- a linha de efeito foi gravada pela tool fechada sobre esse mesmo nome;
- `tool_agent_ok=True` em todas as tentativas;
- o reenvio em terceiro processo adicionou somente o evento de usuário e
  manteve uma linha de efeito.

Resultados por rota:

| Palavra-chave | Autor da confirmação | Agente da tool |
| --- | --- | --- |
| `reserva` | `especialista_reservas` | `especialista_reservas` / `registrar_reserva` |
| `visitante` | `especialista_visitantes` | `especialista_visitantes` / `autorizar_visitante` |

Um ciclo completo H1 de reservas ocupa as linhas 24–94 da saída bruta. O
fechamento real desse ciclo foi:

```text
PHASE1 specialist=especialista_reservas confirmation_id=adk-e220dedc-3b78-4711-9302-719f95db8d3b confirmation_author=especialista_reservas error=none effects=0
PHASE2 confirmed=True authors=especialista_reservas,especialista_reservas,especialista_reservas error=none effects=1
PHASE3 confirmed=True authors=<none> error=none effects=1
TRIAL scenario=H1 specialist=especialista_reservas index=1 decision=approve status=PASS effects=1 confirmation_author=especialista_reservas tool_agent_ok=True errors=none;none;none codes=0,0,0 elapsed_seconds=6.407494
CLEANUP removed=spike_e11_H1_especialista_reservas_approve_1_1_af78caa8ccd64fd3bba52a9b5da0e502.db,efeitos_e11_H1_especialista_reservas_approve_1_1_af78caa8ccd64fd3bba52a9b5da0e502.log,estado_e11_H1_especialista_reservas_approve_1_1_af78caa8ccd64fd3bba52a9b5da0e502.json
```

## H2 — mensagem nova enquanto há confirmação pendente

Resultado estável nas 10 tentativas:

1. o texto novo foi roteado ao mesmo especialista que tinha pedido a
   confirmação (5 reservas e 5 visitantes);
2. a tool não executou por engano: 0 efeitos depois do texto em 10/10;
3. não houve exceção;
4. o novo turno terminou uma nova invocação do especialista;
5. a aprovação posterior do ID original foi anexada como evento de usuário,
   mas gerou zero evento do agente e zero efeito em 10/10.

Portanto, nesta topologia/versão, a confirmação original **não continuou
utilizável** depois de uma mensagem nova. Um exemplo completo está nas linhas
4828–4896 da saída bruta; o trecho decisivo real é:

```text
NEW_TEXT authors=especialista_reservas,especialista_reservas error=none effects=0
PHASE2 confirmed=True authors=<none> error=none effects=0
TRIAL scenario=H2 index=1 status=OBSERVED specialist=especialista_reservas pending_usable=False accidental_execution=False new_text_authors=especialista_reservas,especialista_reservas errors=none;none;none effects_after_text=0 final_effects=0 codes=0,0,0 elapsed_seconds=6.953731
```

Implicação para a aplicação futura, fora desta fatia: a API deve definir uma
política explícita para texto novo com pendência. Se o produto precisar manter
a pendência utilizável, não pode depender deste comportamento do Runner; deve
guardar a pendência no domínio e impedir/serializar nova mensagem ou criar uma
retomada própria. A spec permite qualquer comportamento desde que nada execute
sem confirmação.

## H3 — Custom Agent e suporte de resume no ADK 2.9.2

### Código-fonte instalado lido

- `.venv/Lib/site-packages/google/adk/agents/base_agent.py:83-92`:
  `BaseAgentState` é experimental.
- `base_agent.py:206-247`: `_load_agent_state` e
  `_create_agent_state_event` carregam/serializam o checkpoint; ambos são
  métodos protegidos.
- `base_agent.py:325-440`: `BaseAgent.run_async` chama a implementação custom
  `_run_async_impl`, que a subclasse precisa fornecer.
- `.venv/Lib/site-packages/google/adk/agents/invocation_context.py:267-273`:
  `ctx.is_resumable` deriva de `ResumabilityConfig`.
- `invocation_context.py:299-384`: `set_agent_state` serializa o estado, e
  `populate_invocation_agent_states` o reconstrói dos eventos persistidos.
- `.venv/Lib/site-packages/google/adk/apps/_configs.py:28-46`:
  `ResumabilityConfig` é experimental, best-effort, at-least-once e perde
  estado apenas em memória.
- `.venv/Lib/site-packages/google/adk/runners.py:1030-1073,1173-1259`:
  `run_async(invocation_id=...)` retoma uma invocação interrompida.
- `runners.py:1390-1457`: o Runner persiste cada evento não parcial antes de
  entregá-lo ao consumidor.
- `runners.py:1899-1977`: ao retomar, o Runner recupera a mensagem original,
  popula `agent_states`, escolhe o agente ativo e restaura o branch.
- `.venv/Lib/site-packages/google/adk/agents/llm_agent.py:593-640,950-993`:
  `LlmAgent` já implementa estado final e retomada de subagente/transferência.

O Custom Agent adicionou **11 linhas físicas não vazias**, contadas
automaticamente entre os marcadores `RESUME_SUPPORT_BEGIN/END`: uma classe de
estado, carga do checkpoint, seleção do próximo passo, persistência do próximo
passo e evento de término.

Trecho real completo de H3.1, um ciclo que passa:

```text
CHILD_BEGIN label=H3.1.phase1
COMMAND <REPO>\.venv\Scripts\python.exe -u <REPO>\spike_resume\spike_resume.py --mode custom_phase1 --state <REPO>\spike_resume\estado_e11_H3_1_f057e0d42bd7436187c203fa6eaef3d1.json
<REPO>\spike_resume\spike_resume.py:355: UserWarning: [EXPERIMENTAL] ResumabilityConfig: This feature is experimental and may change or be removed in future versions without notice. It may introduce breaking changes at any time.
  resumability_config=ResumabilityConfig(is_resumable=True),
<REPO>\.venv\Lib\site-packages\google\adk\features\_feature_decorator.py:71: UserWarning: [EXPERIMENTAL] feature FeatureName.AGENT_STATE is enabled.
  check_feature_enabled()
EVENT order=0 id=822f5ecc-1bf6-4c65-a852-6935ca846770 author=especialista_custom timestamp=1790284789.3571434 invocation_id=e-e19dccad-9627-44a5-93e5-98fae6ad8f4a calls=<none> responses=<none> requested=<none> agent_state={"next_step": 2} end_of_agent=None
CUSTOM_CHECKPOINT step1_persisted=true invocation_id=e-e19dccad-9627-44a5-93e5-98fae6ad8f4a effects=1
COMMAND_EXIT code=1
CHILD_END label=H3.1.phase1
KILL_PROOF label=H3.1.phase1 method=Popen.kill checkpoint=persisted returncode=1
CHILD_BEGIN label=H3.1.phase2
COMMAND <REPO>\.venv\Scripts\python.exe -u <REPO>\spike_resume\spike_resume.py --mode custom_phase2 --state <REPO>\spike_resume\estado_e11_H3_1_f057e0d42bd7436187c203fa6eaef3d1.json
<REPO>\spike_resume\spike_resume.py:355: UserWarning: [EXPERIMENTAL] ResumabilityConfig: This feature is experimental and may change or be removed in future versions without notice. It may introduce breaking changes at any time.
  resumability_config=ResumabilityConfig(is_resumable=True),
CUSTOM_RESUME error=none events=1 effects=2
EVENT order=0 id=a4d245c5-8b4e-4910-9c4a-bccaa18c33fd author=especialista_custom timestamp=1790284791.3181057 invocation_id=e-e19dccad-9627-44a5-93e5-98fae6ad8f4a calls=<none> responses=<none> requested=<none> agent_state=<none> end_of_agent=True
SESSION label=custom_after_resume status=found events=3
EVENT order=0 id=feae42c0-a203-4a96-a31a-4bd1802b075b author=user timestamp=1790284789.073419 invocation_id=e-e19dccad-9627-44a5-93e5-98fae6ad8f4a calls=<none> responses=<none> requested=<none> agent_state=<none> end_of_agent=None
EVENT order=1 id=822f5ecc-1bf6-4c65-a852-6935ca846770 author=especialista_custom timestamp=1790284789.3571434 invocation_id=e-e19dccad-9627-44a5-93e5-98fae6ad8f4a calls=<none> responses=<none> requested=<none> agent_state={"next_step": 2} end_of_agent=None
EVENT order=2 id=a4d245c5-8b4e-4910-9c4a-bccaa18c33fd author=especialista_custom timestamp=1790284791.3181057 invocation_id=e-e19dccad-9627-44a5-93e5-98fae6ad8f4a calls=<none> responses=<none> requested=<none> agent_state=<none> end_of_agent=True
SERVICE label=custom_after_resume adjustments=0 persisted_reads=1
COMMAND_EXIT code=0
CHILD_END label=H3.1.phase2
TRIAL scenario=H3 index=1 status=PASS step1=1 step2=1 error=none codes=1,0 elapsed_seconds=4.668474
CLEANUP removed=spike_e11_H3_1_f057e0d42bd7436187c203fa6eaef3d1.db,efeitos_e11_H3_1_f057e0d42bd7436187c203fa6eaef3d1.log,estado_e11_H3_1_f057e0d42bd7436187c203fa6eaef3d1.json
```

Não houve ciclo H1/H3 que falhou. H2 é observacional e refutou apenas a parte
“pendência continua utilizável”; o ciclo representativo está registrado acima
e integralmente na saída bruta.

### O que falta para um Custom Agent participar do H1

`BaseAgent` não possui o campo `tools` nem o fluxo automático de LLM/tools que
`LlmAgent` possui (`llm_agent.py:387,593-640`). Para reproduzir H1 diretamente,
um Custom Agent teria de implementar ou compor:

1. declaração e despacho de tools;
2. emissão/correlação de function calls e function responses;
3. criação de `adk_request_confirmation` e
   `requested_tool_confirmations`;
4. pausa e retomada no ID da confirmação;
5. execução idempotente da tool após aprovação e nenhuma execução após negação;
6. roteamento/transferência equivalente a `transfer_to_agent`;
7. checkpoints de cada passo e tratamento at-least-once.

Delegar isso internamente a um `LlmAgent` reutilizaria o tipo padrão e reduziria
o valor de um Custom Agent como agente principal. Implementar diretamente é
código adicional relevante e acoplado ao protocolo experimental do ADK.

## H4 — comparação e decisão

| Critério | `LlmAgent` padrão | Custom Agent |
| --- | --- | --- |
| Resume medido | H1: 20/20 por especialista em aprovar+reenviar e 20/20 em negar | H3: 20/20 entre dois passos com checkpoint explícito |
| Linhas adicionais específicas de resume | 0 no agente; só configuração compartilhada do `App` | 11 |
| API interna/experimental | `ResumabilityConfig` experimental; a implementação interna fica encapsulada pelo ADK | `ResumabilityConfig` + `BaseAgentState` experimentais e chamadas diretas aos métodos protegidos `_load_agent_state`/`_create_agent_state_event` |
| `require_confirmation` | Nativo e medido em H1 | Não oferecido por `BaseAgent`; exige implementação/composição relevante |
| `transfer_to_agent` | Nativo e medido principal → 2 especialistas | Não oferecido automaticamente; exige roteamento próprio/composição |
| Risco de manutenção | Médio: recurso experimental, mas caminho padrão do ADK | Alto: checkpoint/protocolo manual e dependência de métodos protegidos |

### DECISÃO — PROPOSTA para aprovação do owner (§14)

**PROPOSTA:** usar `LlmAgent` padrão tanto para o agente principal quanto para
`especialista_reservas` e `especialista_visitantes`.

Motivo: foi a única opção que combinou, sem código específico de resume no
agente, transferência principal → dois especialistas, `require_confirmation`,
aprovação em processo novo, reenvio sem duplicação e negação sem efeito. O
Custom Agent retomou 20/20 quando recebeu 11 linhas explícitas de checkpoint,
mas não fornece o fluxo de confirmação/transferência de H1 e depende
diretamente de APIs experimentais e métodos protegidos. Não apareceu uma
necessidade concreta que compense esse custo e risco.

Esta decisão não foi implementada na aplicação; depende de aprovação do owner.

## Reprodução e saída real completa

Executar a partir da raiz `mba-ia-desafio-criacao-agente/`:

```powershell
.\.venv\Scripts\python.exe spike_resume\spike_resume.py
```

Saída bruta integral: `spike_resume/RESULTADO-BRUTO-RESUME.txt`
(1.187.787 bytes, 6.041 linhas). O comando terminou com exit code 0.

## Custo observado

- Baseline congelado da E11: 18–35 min e 30–60k tokens, fonte:
  `docs2/plan/COST-LEDGER.csv`, linha E11.
- Matriz automatizada: 683,013564 s (11 min 23,014 s), fonte:
  `SPIKE_COMPLETED` na última linha da saída bruta.
- Tempo de executor até o fechamento das evidências: 1.120,521 s
  (18 min 40,521 s), de `2026-09-24T21:06:55.9330995Z` a
  `2026-09-24T21:25:36.4544900Z`; fonte: subtração de dois
  `DateTimeOffset::UtcNow` impressos pelo PowerShell.
- Tokens observados: **não mensurados**. A interface da sessão não expôs um
  contador de tokens verificável ao executor; pela Constitution §12, a
  estimativa do baseline não foi reapresentada como valor observado.

## Artefatos gerados e o que ignorar/apagar

| Arquivo | Tamanho | Destino |
| --- | ---: | --- |
| `spike_resume/spike_resume.py` | 36.206 bytes | Código reproduzível do spike; candidato a versionar após decisão do owner |
| `spike_resume/spike_servico_ordenado.py` | 3.365 bytes | Cópia declarada da E9; candidato a versionar com o spike |
| `spike_resume/RESULTADO-RESUME.md` | 18.490 bytes | Resultado consolidado; candidato a versionar após decisão do owner |
| `spike_resume/RESULTADO-BRUTO-RESUME.txt` | 1.187.787 bytes | Evidência bruta completa; não precisa ir para o git se o owner preferir evitar artefato grande, mas deve ser preservada até o delegante reexecutar |
| `spike_resume/__pycache__/spike_resume.cpython-312.pyc` | 53.565 bytes | Cache descartável; não versionar/apagar |
| `spike_resume/__pycache__/spike_servico_ordenado.cpython-312.pyc` | 4.418 bytes | Cache descartável; não versionar/apagar |

Temporários já apagados pelo harness: `spike_e11_*.db`,
`spike_e11_*.db-wal`, `spike_e11_*.db-shm`, `efeitos_e11_*.log` e
`estado_e11_*.json`. Não foi editado `.gitignore`.

`spike_resume/__pycache__/`, se criado localmente pelo Python, é cache
descartável e não deve ir para o git. Nenhum arquivo fora de `spike_resume/**`
foi alterado por esta fatia.
