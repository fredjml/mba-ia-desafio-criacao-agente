# E9-B3 — Contornos do bug de ordem de eventos

## Hipóteses e critérios (registrados antes da execução)

| Opção | Hipótese | Sucesso | Refutação |
| --- | --- | --- | --- |
| 1 — agente único | Sem autor divergente, o fallback do roteador continuará no agente raiz correto | Aprovação executa exatamente uma vez em 40/40 | Qualquer falha em 40 |
| 2 — SessionService com timestamps monotônicos | Tornar o timestamp estritamente crescente na ordem de append fará a releitura SQLite preservar a ordem funcional | Aprovação executa exatamente uma vez em 40/40 | Qualquer falha em 40 |
| 3 — ADK anterior | Alguma versão anterior não contém simultaneamente emissão confirmação→response e releitura por timestamp/id | Leitura estática encontra ao menos uma versão sem a combinação; somente então ela será medida em 40/40 no venv isolado | Todas as versões 2.9.1, 2.9.0, 2.8.0 e 2.2.0 contêm a combinação |

Para cada opção dinâmica que atingir 0/40, serão medidos N=20 para negação,
reenvio e ID inexistente.

## Resultado

Ambiente: Windows, Python 3.12.10, `google-adk==2.9.2` no `.venv` principal
somente para execução; nenhum pacote nele foi instalado ou atualizado. Modelo
stub local, um processo, SQLite novo por tentativa e nenhum reinício.

### Aprovação — N=40

| Opção | ADK | Falhas | Conclusão |
| --- | --- | ---: | --- |
| 1 — agente único | 2.9.2 | **0/40** | Passou o critério focal, mas foi rejeitada pelo cenário 4 abaixo |
| 2 — timestamps monotônicos | 2.9.2 | **0/40** | Passou o critério focal e os cenários 3/4/5 |
| 3 — versão anterior | 2.8.0 | **28/40** | Reprovada dinamicamente; regressões não executadas |

### Cenários 3, 4 e 5 — N=20

| Contorno | Negar não executa | Reenvio não reexecuta | ID inexistente gera `ValueError`, sem executar |
| --- | ---: | ---: | ---: |
| 1 — agente único | 0/20 falhas | **20/20 falhas**; contador terminou em 2 | 0/20 falhas |
| 2 — timestamps monotônicos | 0/20 falhas | 0/20 falhas | 0/20 falhas |
| 3 — ADK 2.8.0 | não executado | não executado | não executado |

Assim, a opção 1 não é um contorno válido do conjunto de garantias: ela
aprova 40/40, mas reexecuta a tool em 20/20 reenvios.

### Opção 3 — leitura estática

O comando `python -m pip index versions google-adk` confirmou 2.9.2 como a
versão mais recente e listou todas as versões solicitadas. Os quatro wheels
foram baixados com `--no-deps` e extraídos como ZIP, sem instalação no `.venv`
principal.

| Versão | Ordem no fluxo assíncrono | Ordem na releitura DB | Bug estático |
| --- | --- | --- | --- |
| 2.9.1 | confirmação linha 936; response linha 939 | `(timestamp DESC, id DESC)` linhas 729-730; `reversed` 757 | sim |
| 2.9.0 | confirmação linha 936; response linha 939 | `(timestamp DESC, id DESC)` linhas 729-730; `reversed` 757 | sim |
| 2.8.0 | confirmação linha 1667; response linha 1670 | `(timestamp DESC, id DESC)` linhas 696-697; `reversed` 724 | sim |
| 2.2.0 | confirmação linha 1205; response linha 1208 | `timestamp DESC` linha 532; `reversed` 556 | sim; sem desempate por ID |

Arquivos citados, sob
`spike_confirmacao/adk_antigo/extracted/<versão>/google/adk/`:
`flows/llm_flows/base_llm_flow.py` e
`sessions/database_session_service.py`.

Houve um atrito de instrumentação: a primeira busca automática capturou o
`yield function_response_event` do caminho live em 2.8.0/2.2.0, fora de
`_postprocess_handle_function_calls_async`, e produziu falso negativo. Naquele
estado, o gate condicional apontou 2.8.0 como candidata; por isso o venv isolado
foi criado, o `git check-ignore -v` confirmou `.gitignore:2:.venv/`, houve
`--dry-run`, e 2.8.0 foi instalado somente ali. A leitura focal corrigiu o
coletor para restringi-lo ao bloco assíncrono. O teste dinâmico independente
também refutou a candidata: 28/40 falhas. A versão 2.2.0 não foi instalada.

### Evidência real

```text
SUMMARY option=1_single_agent scenario=approve failures=0/40
SUMMARY option=1_single_agent scenario=3 failures=0/20
SUMMARY option=1_single_agent scenario=4 failures=20/20
SUMMARY option=1_single_agent scenario=5 failures=0/20
SUMMARY option=2_monotonic_service scenario=approve failures=0/40
SUMMARY option=2_monotonic_service scenario=3 failures=0/20
SUMMARY option=2_monotonic_service scenario=4 failures=0/20
SUMMARY option=2_monotonic_service scenario=5 failures=0/20
TRIAL option=1_single_agent scenario=4 index=1 status=FAIL counter=2 errors=none;none;none
TRIAL option=2_monotonic_service scenario=approve index=1 status=PASS counter=1 initial_error=none approval_error=none timestamp_adjustments=1
TRIAL option=2_monotonic_service scenario=4 index=1 status=PASS counter=1 errors=none;none;none
STATIC version=2.9.1 confirmation_yield=936 response_yield=939 async_block=914 order_by=729 reverse=757 orders_timestamp=True orders_id=True bug_present=True
STATIC version=2.9.0 confirmation_yield=936 response_yield=939 async_block=914 order_by=729 reverse=757 orders_timestamp=True orders_id=True bug_present=True
STATIC version=2.8.0 confirmation_yield=1667 response_yield=1670 async_block=1645 order_by=696 reverse=724 orders_timestamp=True orders_id=True bug_present=True
STATIC version=2.2.0 confirmation_yield=1205 response_yield=1208 async_block=1186 order_by=532 reverse=556 orders_timestamp=True orders_id=False bug_present=True
STATIC_SUMMARY version_2.9.1_bug=True version_2.9.0_bug=True version_2.8.0_bug=True version_2.2.0_bug=True candidates=<none>
FINAL option1={'approve_failures': 0, 'scenario_3_failures': 0, 'scenario_4_failures': 20, 'scenario_5_failures': 0} option2={'approve_failures': 0, 'scenario_3_failures': 0, 'scenario_4_failures': 0, 'scenario_5_failures': 0} database_leftovers=<none>
SPIKE_COMPLETED utc=2026-09-24T19:55:09.053067+00:00 elapsed_seconds=93.441687 raw_output=RESULTADO-BRUTO-CONTORNOS.txt
```

```text
TRIAL adk=2.8.0 scenario=2 index=1 status=PASS counter=1 errors=none;none
TRIAL adk=2.8.0 scenario=2 index=2 status=FAIL counter=0 errors=none;none
SUMMARY adk=2.8.0 scenario=2 failures=28/40
REGRESSION_SKIPPED reason=approval_not_0_of_40
FINAL results={2: 28} database_leftovers=<none>
SPIKE_COMPLETED utc=2026-09-24T19:52:49.030079+00:00 elapsed_seconds=12.523672
```

Saídas brutas completas:

- `spike_confirmacao/RESULTADO-BRUTO-CONTORNOS.txt`;
- `spike_confirmacao/adk_antigo/RESULTADO-BRUTO-ADK-2.8.0.txt`;
- `spike_confirmacao/adk_antigo/RESULTADO-BRUTO-AMBIENTE-ADK-2.8.0.txt`.

Comandos reproduzíveis:

```powershell
.\.venv\Scripts\python.exe spike_confirmacao\spike_contornos.py
git check-ignore -v "spike_confirmacao/adk_antigo/.venv/"
python -m uv venv --python 3.12 "spike_confirmacao\adk_antigo\.venv"
python -m uv pip install --dry-run --python "spike_confirmacao\adk_antigo\.venv\Scripts\python.exe" "google-adk[db]==2.8.0"
python -m uv pip install --python "spike_confirmacao\adk_antigo\.venv\Scripts\python.exe" "google-adk[db]==2.8.0"
.\spike_confirmacao\adk_antigo\.venv\Scripts\python.exe spike_confirmacao\adk_antigo\spike_adk_2_8_0.py
```

## Riscos

### Opção 1

- Elimina a topologia principal→especialistas planejada e não atende o desenho
  que sustenta AC-05.
- Falhou a propriedade de não reexecução: 20/20 reenvios executaram novamente.
- Não deve ser adotada apesar de 0/40 na primeira aprovação.

### Opção 2

- Não usa método privado do ADK. Sobrescreve a API pública `append_event` e
  altera o campo público `Event.timestamp`; ainda assim depende da semântica
  interna de ordenação do ADK 2.9.2.
- O dicionário do último timestamp é local ao objeto. Ele é suficiente para o
  processo único medido, mas não prova reinício nem múltiplos processos.
- O ajuste ocorre antes do lock interno de `DatabaseSessionService`; chamadas
  concorrentes podem observar o mesmo último timestamp. O contorno precisa de
  sincronização por sessão ou mecanismo persistido antes de ser candidato a
  produção.
- Muda timestamps em microssegundos, o que pode afetar auditoria temporal.
- Exige custo de manutenção e repetição de E9-A/B/B2/B3, reinício e concorrência
  a cada mudança de ADK.

### Opção 3

- Fixar versão anterior impacta R9/fixação e amplia manutenção e exposição a
  bugs/correções já superados.
- Todas as versões lidas mantêm o bug. 2.8.0 ainda falhou 28/40 ao vivo.
- Qualquer troca de versão exige repetir as leituras e todas as medições
  E9-A/B/B2, além dos testes de reinício e concorrência.

## RECOMENDAÇÃO — PROPOSTA para aprovação do owner (§14)

**PROPOSTA:** levar somente a opção 2 para a próxima fatia de validação, sem
implementá-la ainda na aplicação. Ela foi a única com aprovação 0/40 e
cenários 3/4/5 em 0/20 cada, mantendo a topologia original.

A aprovação deve ser condicionada a: (1) tornar a monotonicidade segura sob
concorrência e persistente após novo objeto/processo; (2) repetir reinício real;
(3) repetir os gates A/B/B2 e os cenários de concorrência; e (4) manter a tool
idempotente, pois resumability continua at-least-once.
