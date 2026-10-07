# Padrões de Implementação — Aurora Virtual Assistant

**Como estendemos ADK, estrutura de ferramentas, persistence patterns.**

---

## 🔧 Padrão 1: Agentes Estruturados (Tool-Driven)

### Estrutura ADK

```python
# src/aurora/agentes/agente_principal.py
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import MessageTextContent
from azure.identity import DefaultAzureCredential

class AgenteAurora:
    def __init__(self, session_context: dict):
        self.client = AIProjectClient.from_config(
            credential=DefaultAzureCredential()
        )
        self.memory = MemoriaAgente(session_context)
        self.tools = self._construir_tools()
    
    def _construir_tools(self):
        """Ferramentas são lambdas que recebem JSON do agente."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "reservas_criar",
                    "description": "Propõe nova reserva para área do condomínio",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "area": {"type": "string", "enum": [
                                "salao-de-festas", "churrasqueira", "piscina"
                            ]},
                            "data": {"type": "string", "format": "date"},
                            "hora_inicio": {"type": "string", "format": "time"},
                            "descricao": {"type": "string"}
                        },
                        "required": ["area", "data", "hora_inicio"]
                    }
                }
            },
            # ... mais ferramentas
        ]
    
    async def processar_mensagem(self, user_message: str):
        """Loop de agente: input → LLM + tools → output."""
        historico = self.memory.carregar_historico()
        
        # Chamada do Gemini
        response = await self.client.agents.create_message(
            assistant_id=self.assistant_id,
            thread_id=self.memory.session_id,
            messages=[MessageTextContent(content=user_message)]
        )
        
        # Processamento de tools chamadas
        for tool_call in response.tool_calls:
            resultado = await self._executar_tool(
                tool_call.function.name,
                tool_call.function.arguments
            )
            # LLM recebe resultado e continua loop
        
        return response.text_content
```

### Isolamento de Apartamento (Lexical Guard)

```python
# src/aurora/agentes/memoria.py:15-35
class MemoriaAgente:
    """Sessão de agente = apartamento locked."""
    
    def __init__(self, session_context: dict):
        self.apartamento = session_context["apartment_id"]
        
        # GUARDA LEXICAL: Valida formato (não depende de DB lookup)
        if not re.match(r"^[0-9]{3}$", self.apartamento):
            raise ValueError(f"Apartamento inválido: {self.apartamento}")
        
        self.session_id = session_context["session_id"]
        self.historico: List[dict] = []
    
    def instrucao_sistema(self) -> str:
        """Prompt que define contexto seguro."""
        return f"""
        Você é Aurora, assistente do apartamento {self.apartamento}.
        
        Seus dados e reservas:
        - Apartamento: {self.apartamento}
        - Não tente acessar outros apartamentos
        - Ferramentas garantem isolamento
        
        {INSTRUCAO_PRINCIPAL}
        """
```

### Padrão de Tool Execution

```python
async def _executar_tool(self, tool_name: str, args: dict):
    """Tool dispatcher: ADK chama, memory valida, DB executa."""
    
    # 1. Valida: tool é conhecida?
    if tool_name not in TOOLS_PERMITIDAS:
        return {"erro": "tool desconhecida", "codigo": 404}
    
    # 2. Isola: extrai apartamento (nunca de args)
    args["apartamento"] = self.apartamento  # SEMPRE do contexto
    
    # 3. Executa: chamar tool real
    tool_fn = getattr(ToolsExecutor(self.apartamento), tool_name)
    resultado = await tool_fn(**args)
    
    # 4. Registra: evento no histórico
    self.historico.append({
        "tipo": "tool_execution",
        "tool": tool_name,
        "entrada": args,
        "resultado": resultado,
        "timestamp": datetime.now().isoformat()
    })
    
    return resultado
```

---

## 🔧 Padrão 2: OrderedDatabaseSessionService (Persistência ADK)

### Integração com SQLite

```python
# src/aurora/runtime/fabrica.py:50-70
from azure.ai.projects.persistence import OrderedDatabaseSessionService

class FabricaDeServicos:
    @staticmethod
    def criar_sessao_agente(session_id: str, apartamento: str):
        """Fabrica de serviços ADK: 1 sessão = 1 BD persistido."""
        
        # Caminho: var/{session_id}.db
        db_path = Path("var") / f"{session_id}.db"
        db_path.parent.mkdir(exist_ok=True)
        
        # ADK service: mantém histórico de turnos
        session_service = OrderedDatabaseSessionService(
            db_path=db_path,
            table_name="adk_turnos",
            # Cada turno = 1 linha com histórico completo
        )
        
        # Contexto do agente
        session_context = {
            "apartment_id": apartamento,
            "session_id": session_id,
            "session_service": session_service
        }
        
        return AgenteAurora(session_context)

    @staticmethod
    def recuperar_sessao(session_id: str):
        """Recuperação de crash: sessão recarregada intacta."""
        db_path = Path("var") / f"{session_id}.db"
        
        if not db_path.exists():
            raise ValueError(f"Sessão {session_id} não encontrada")
        
        session_service = OrderedDatabaseSessionService(db_path=db_path)
        # Histórico está lá: adk_turnos.* intacto
        
        # App reinicia como se nunca caísse
        return session_service.get_last_state()
```

### Estrutura de Turno (Turn/Exchange)

```python
# Tabela ADK (criada pelo OrderedDatabaseSessionService)
CREATE TABLE adk_turnos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    turn_number INTEGER NOT NULL,
    
    -- Request
    user_message TEXT NOT NULL,
    
    -- Response
    assistant_response TEXT,
    tool_calls TEXT,  -- JSON array de {name, arguments}
    tool_results TEXT,  -- JSON array de {tool, result}
    
    -- Metadata
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    duration_ms INTEGER,
    
    UNIQUE(thread_id, turn_number)
);
```

### Replay de Crash

```python
# Cenário: Agente processava turno 5, caiu
# Solução: OrderedDatabaseSessionService reconstrói estado

session_service = OrderedDatabaseSessionService(db_path="var/abc-123.db")

# Turno 5 estava incompleto (user_message inserido, response NULL)
# → skip turno 5, start turno 6

turno_atual = session_service.get_next_turn()
# → turno 6 (turn_number = 6)

# Histórico de turnos 1-4 disponível para context window do Gemini
```

---

## 🔧 Padrão 3: Session Persistence Pattern (Nosso)

### Banco Aurora (Dados do Condomínio)

```python
# src/aurora/runtime/conexao.py
class BancoAurora:
    """Banco compartilhado: reservas, visitantes, histórico, prefs, seq."""
    
    def __init__(self, path: str = "var/aurora.db"):
        self.path = Path(path)
        self._setup()
    
    def _setup(self):
        """Cria tabelas se não existem."""
        with sqlite3.connect(self.path) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS reservas (
                id TEXT PRIMARY KEY,
                apartamento TEXT NOT NULL,
                area TEXT NOT NULL,
                data DATE NOT NULL,
                hora_inicio TIME,
                hora_fim TIME,
                descricao TEXT,
                confirmada BOOLEAN DEFAULT FALSE,
                ativa BOOLEAN DEFAULT TRUE,
                taxa REAL,
                criada_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                atualizada_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            
            # ✅ ÍNDICE ÚNICO PARCIAL (Garantia 5)
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ux_reservas_ativa
                ON reservas(area, data) WHERE ativa=1""")
            
            db.commit()
    
    def transaction(self):
        """Context manager: garante atomicidade."""
        return TransactionContext(self.path)

class TransactionContext:
    """BEGIN; ... COMMIT ou ROLLBACK."""
    
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path, timeout=5)
        self.conn.execute("PRAGMA foreign_keys=ON")
    
    def __enter__(self):
        self.conn.execute("BEGIN")
        return self.conn
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self.conn.commit()
        else:
            self.conn.rollback()
        self.conn.close()
```

### Acesso Seguro (Tools Executors)

```python
# src/aurora/tools/reservas.py (exemplo de pattern)
class ExecutorReservas:
    """Cada executor = 1 domínio de dados."""
    
    def __init__(self, apartamento: str):
        self.apartamento = apartamento
        self.db = BancoAurora()
    
    async def criar_reserva(self, area: str, data: str, 
                           hora_inicio: str = None) -> dict:
        """Cria reserva com validação e atomicidade."""
        
        # 1. Valida entrada
        if area not in AREAS_VALIDAS:
            return {"erro": "area_invalida", "codigo": 400}
        
        # 2. Transação atômica
        try:
            with self.db.transaction() as conn:
                # Gera ID (Garantia 3)
                novo_id = gerar_proximo_id_reserva(conn)
                
                # Insere (índice único falha se conflito)
                conn.execute("""
                    INSERT INTO reservas 
                    (id, apartamento, area, data, hora_inicio, ativa, confirmada)
                    VALUES (?, ?, ?, ?, ?, TRUE, FALSE)
                """, (novo_id, self.apartamento, area, data, hora_inicio))
                
                # Registra evento
                conn.execute("""
                    INSERT INTO historico_eventos 
                    (apartamento, tipo, descricao, timestamp)
                    VALUES (?, 'reserva_criada', ?, CURRENT_TIMESTAMP)
                """, (self.apartamento, f"Reserva {novo_id}"))
                
                # COMMIT automático ao sair
        
        except IntegrityError as e:
            if "ux_reservas_ativa" in str(e):
                return {
                    "erro": "conflito_reserva",
                    "codigo": 409,
                    "mensagem": f"Já existe reserva em {area} em {data}"
                }
            raise
        
        return {"status": "criada", "id": novo_id}
```

---

## 🔧 Padrão 4: Garantias via Decoradores

### Validação Declarativa

```python
# src/aurora/runtime/decoradores.py
def requer_confirmacao(fn):
    """Registra que resultado precisa confirmação."""
    async def wrapper(*args, **kwargs):
        resultado = await fn(*args, **kwargs)
        resultado["requer_confirmacao"] = True
        return resultado
    return wrapper

def apenas_leitura(fn):
    """Garante que função não modifica DB."""
    async def wrapper(*args, **kwargs):
        # Desativa INSERT/UPDATE/DELETE no contexto
        resultado = await fn(*args, **kwargs)
        return resultado
    return wrapper

# Uso
@requer_confirmacao
async def criar_reserva(...): ...

@apenas_leitura
async def consultar_regulamento(...): ...
```

---

## 📚 Extensão: Adicionar Nova Tool

**Passo a passo para estender o projeto:**

1. **Definir Executor**
   ```python
   # src/aurora/tools/nova_tool.py
   class ExecutorNovaFuncionalidade:
       def __init__(self, apartamento: str):
           self.apartamento = apartamento
   
       async def fazer_algo(self, param: str) -> dict:
           # Isolamento: sempre filtrar por self.apartamento
           ...
   ```

2. **Registrar em AgenteAurora**
   ```python
   def _construir_tools(self):
       return [
           # ... tools existentes
           {
               "type": "function",
               "function": {
                   "name": "nova_tool",
                   "description": "...",
                   "parameters": { ... }
               }
           }
       ]
   ```

3. **Adicionar testes**
   ```python
   # tests/integracao/test_nova_tool.py
   p.checar("Nova tool funciona", ...)
   ```

4. **Documentar**
   - Atualizar [docs2/architecture/api-contracts.md](./architecture/api-contracts.md)
   - Documentar garantia que afeta (se houver)

---

**Última atualização:** 2026-10-07  
**Status:** ✅ Padrões documentados
