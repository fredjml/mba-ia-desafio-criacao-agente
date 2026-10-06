# Leitura do código instalado — Google ADK

## Escopo e versão

- Fatia: E9-A (`SPIKE-CONFIRM`), somente leitura do código instalado; nenhum modelo foi chamado.
- Ambiente: Python 3.12.10, `google-adk==2.9.2`.
- Fonte da versão: `.venv/Lib/site-packages/google/adk/version.py:15-16`.
- Esta leitura sustenta AC-07..AC-13, AC-19 e AC-20, mas não substitui os experimentos de confirmação/reinício das fatias B/C.

## 1. Como funciona a confirmação de tool

1. `FunctionTool(..., require_confirmation=...)` aceita `bool` ou callable; o callable recebe os argumentos e decide dinamicamente se a chamada exige confirmação (`.venv/Lib/site-packages/google/adk/tools/function_tool.py:100-124`). `check_require_confirmation` executa essa decisão (`function_tool.py:196-206`).
2. Antes de chamar a função real, `run_async` verifica a confirmação (`function_tool.py:267-269`):
   - se ela é exigida e ainda não existe em `tool_context`, chama `request_confirmation`, marca `skip_summarization` e retorna sem executar a função (`function_tool.py:271-290`);
   - se a resposta existe mas `confirmed` é falsa, retorna rejeição sem executar (`function_tool.py:291-292`);
   - somente nos demais casos chama a função real (`function_tool.py:294-295`).
3. `Context.request_confirmation` grava um `ToolConfirmation` em `EventActions.requested_tool_confirmations`, indexado pelo ID da function call original (`.venv/Lib/site-packages/google/adk/agents/context.py:852-882`; `.venv/Lib/site-packages/google/adk/events/event_actions.py:155-159`). O modelo contém `hint`, `confirmed` (padrão `False`) e `payload` (`.venv/Lib/site-packages/google/adk/tools/tool_confirmation.py:28-45`).
4. O runtime transforma essa solicitação em uma function call especial `adk_request_confirmation`. Ela contém a function call original, recebe um novo ID e é marcada como long-running; o evento é escrito com o nome do agente atual como autor (`.venv/Lib/site-packages/google/adk/flows/llm_flows/functions.py:223-265`).
5. O cliente responde com uma `FunctionResponse` para o ID de `adk_request_confirmation`. O processador lê a última resposta do usuário, valida a ferramenta, a exigência de confirmação e a igualdade de nome/argumentos com a chamada original (`.venv/Lib/site-packages/google/adk/flows/llm_flows/request_confirmation.py:270-353`). Depois, reexecuta a function call original injetando o `ToolConfirmation` (`request_confirmation.py:359-367`). A proteção também ignora uma confirmação já consumida na mesma execução (`request_confirmation.py:294-322`).

Conclusão fechada pela leitura: `require_confirmation=True` impede a execução inicial e a negação; a aprovação é representada por uma resposta estruturada ligada aos IDs registrados nos eventos, não por texto livre da conversa.

Limite: a leitura mostra deduplicação dentro do fluxo do ADK, mas não prova sozinha o contrato HTTP do projeto (`409` para ID inexistente/respondido) nem a atomicidade ponta a ponta. Esses controles continuam responsabilidade da API e das fatias B/C.

## 2. Qual agente recebe a retomada

- O `Runner` procura a function call correspondente à última `FunctionResponse`. Com resumability habilitada, retorna o agente cujo nome é o `author` daquele evento (`.venv/Lib/site-packages/google/adk/agents/_agent_router.py:114-134`).
- O evento `adk_request_confirmation` é criado com o agente atual como autor (`.venv/Lib/site-packages/google/adk/flows/llm_flows/functions.py:259-264`).
- Há uma segunda barreira: ao resolver a confirmação, se a function call original foi escrita por outro agente, o processador a ignora para que o processador daquele agente a trate (`.venv/Lib/site-packages/google/adk/flows/llm_flows/request_confirmation.py:151-164`).

Conclusão fechada pela leitura para ADK 2.9.2: com `App` resumable, a retomada é roteada ao agente autor da chamada de confirmação; a confirmação da tool só é consumida pelo agente autor da function call original. Na topologia normal, ambos são o agente que solicitou a confirmação.

Sem resumability, o roteamento não fica preso ao autor da function call: o algoritmo cai nas regras de agente raiz/último agente transferível e, por fim, no agente raiz (`_agent_router.py:120-160`). Isso explica estruturalmente o risco F3/R2, mas a combinação exata de topologia e flags ainda precisa ser exercitada.

## 3. SessionService existentes e persistência da pendência

As implementações públicas exportadas pelo pacote são:

- `InMemorySessionService`;
- `DatabaseSessionService`;
- `VertexAiSessionService`.

Fonte: `.venv/Lib/site-packages/google/adk/sessions/__init__.py:26-39`.

### InMemorySessionService

Mantém sessões, eventos e estados em dicionários do processo (`.venv/Lib/site-packages/google/adk/sessions/in_memory_session_service.py:62-76`) e anexa os eventos à estrutura em memória (`in_memory_session_service.py:323-373`). Portanto, preserva a pendência apenas durante a vida daquele objeto/processo; não sobrevive a reinício.

### DatabaseSessionService

É explicitamente um serviço de sessão baseado em banco (`.venv/Lib/site-packages/google/adk/sessions/database_session_service.py:285-286`). `append_event` grava cada evento como `StorageEvent` e confirma a transação (`database_session_service.py:866-879,963-985`). No schema v1, `StorageEvent.from_event` serializa o `Event` completo em JSON e `to_event` o reconstrói (`.venv/Lib/site-packages/google/adk/sessions/schemas/v1.py:177-203,225-254`). `get_session` recarrega e converte os eventos armazenados (`database_session_service.py:683-715,725-757`).

Como a pendência está nos eventos — `requested_tool_confirmations` e a function call `adk_request_confirmation` — a leitura fecha que `DatabaseSessionService` persiste os dados necessários à pendência. O pacote-base instalado, porém, não trouxe `sqlalchemy`; a importação pública converte a ausência no erro do extra `db` (`.venv/Lib/site-packages/google/adk/sessions/__init__.py:51-56`; `database_session_service.py:332-337`). A instalação/configuração desse extra não pertence à fatia A.

### VertexAiSessionService

É um backend remoto do Vertex AI Agent Engine (`.venv/Lib/site-packages/google/adk/sessions/vertex_ai_session_service.py:121-125`). Ele tenta gravar o evento completo em `raw_event` (`vertex_ai_session_service.py:470-479`) e prioriza esse conteúdo ao recarregar (`vertex_ai_session_service.py:580-599`). Existe fallback para armazenamento por campos legados, e esse mapa não inclui `requested_tool_confirmations` (`vertex_ai_session_service.py:405-425,481-482`).

Conclusão fechada pela leitura: `DatabaseSessionService` tem caminho local explícito para persistir e reconstruir o evento completo; `InMemorySessionService` não sobrevive ao processo. O caminho moderno de `VertexAiSessionService` também tenta persistir o evento completo, mas o comportamento real do serviço remoto e de seu fallback não é comprovável sem chamada externa.

## 4. O que `App(resumable)` muda

`App.resumability_config` aplica a configuração a todos os agentes (`.venv/Lib/site-packages/google/adk/apps/app.py:84-94`). `ResumabilityConfig(is_resumable=True)` habilita pausa em function call long-running e retomada a partir do último evento; a própria classe avisa que a retomada é best-effort, at-least-once, exige tool idempotente e perde estado temporário/em memória (`.venv/Lib/site-packages/google/adk/apps/_configs.py:28-46`).

No `Runner`, a configuração:

- habilita o roteamento da `FunctionResponse` para o autor da function call correspondente (`.venv/Lib/site-packages/google/adk/agents/_agent_router.py:114-134`);
- permite resolver o `invocation_id` a partir dos IDs das function responses e montar um contexto retomado (`.venv/Lib/site-packages/google/adk/runners.py:500-533,628-649`);
- permite retomar sem nova mensagem quando há `invocation_id`; sem resumability, isso é rejeitado (`runners.py:1191-1240`);
- na retomada, recarrega estados da invocação e escolhe o agente apropriado pelo histórico (`runners.py:1899-1977`).

`resumable` não torna o `SessionService` persistente, não torna a tool exactly-once e não preserva estado temporário. Persistência vem do serviço de sessão; idempotência continua sendo obrigação da aplicação.

## 5. Comportamento esperado após reinício de processo

Fechado pela leitura:

- `InMemorySessionService`: perde sessões/eventos e, portanto, a confirmação pendente ao encerrar o processo.
- `DatabaseSessionService` apontando para um banco durável: um novo processo pode recarregar os eventos completos, inclusive os dados usados para localizar a confirmação, e o `Runner` resumable contém o caminho de reconstrução e roteamento descrito acima.
- `App(resumable)` sozinho não persiste nada.

Não fechado pela leitura:

- que a combinação concreta `DatabaseSessionService` + SQLite + `App(resumable)` executa a tool aprovada exatamente uma vez depois de matar e iniciar outro processo;
- que toda topologia principal/especialista mantém o mesmo autor/branch esperado;
- que o backend Vertex remoto aceita e devolve `raw_event` sem cair no fallback;
- o comportamento transacional da rota própria do projeto para IDs inexistentes, negados ou já consumidos.

Esses itens são **hipóteses a testar nas fatias B/C**, com processo realmente reiniciado e sem chamada de modelo desnecessária na fatia A.

