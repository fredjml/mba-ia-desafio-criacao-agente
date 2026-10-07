# Contratos de API — Aurora Virtual Assistant

**Definição de rotas HTTP, esquemas de request/response, códigos de status.**

---

## 🚀 Base URL

```
http://localhost:8000  (desenvolvimento)
https://aurora.residencial.local  (produção)
```

---

## 📡 Rotas

### 1. **Enviar Mensagem para Agente**

**POST** `/chat`

Envia uma mensagem de usuário e obtém resposta da Aurora com possíveis ações.

**Request Headers:**
```
Content-Type: application/json
X-Apartment: "101"           # Apartamento do usuário (obrigatório)
X-Session-ID: "abc-123-def"  # UUID da sessão (obrigatório)
```

**Request Body:**
```json
{
  "message": "Quero reservar o salão de festas para 10 de março"
}
```

**Response 200 OK:**
```json
{
  "message_id": "msg-5f8c9d2e",
  "assistant_response": "Entendi! Vou verificar a disponibilidade do salão para 10 de março...",
  "confirmations_pending": [
    {
      "id": "conf-1",
      "action": "reserva_criada",
      "description": "Reservar salão de festas para 2030-03-10",
      "requires_response": true
    }
  ],
  "state": {
    "session_id": "abc-123-def",
    "apartment": "101",
    "messages_count": 5,
    "pending_confirmations": 1
  }
}
```

**Response 400 Bad Request:**
```json
{
  "error": "missing_apartment",
  "message": "Header X-Apartment é obrigatório"
}
```

**Response 409 Conflict (Sem Disponibilidade):**
```json
{
  "error": "reservation_conflict",
  "message": "Salão já está reservado em 2030-03-10",
  "existing_reservation": {
    "id": "RSV-4820",
    "apartment": "203",
    "area": "salao-de-festas",
    "date": "2030-03-10"
  }
}
```

---

### 2. **Confirmar Ação Pendente**

**POST** `/confirmations/{confirmation_id}`

Confirma uma ação que o agente propôs (ex: autorizar visitante).

**Request Body:**
```json
{
  "confirmed": true
}
```

**Response 200 OK:**
```json
{
  "confirmation_id": "conf-1",
  "status": "confirmed",
  "action": "reserva_criada",
  "result": {
    "reservation_id": "RSV-4821",
    "area": "salao-de-festas",
    "date": "2030-03-10",
    "time_slot": "14:00-18:00"
  }
}
```

**Response 404 Not Found:**
```json
{
  "error": "confirmation_not_found",
  "confirmation_id": "conf-999"
}
```

**Response 410 Gone (Confirmação expirou):**
```json
{
  "error": "confirmation_expired",
  "message": "Esta confirmação expirou (TTL de 5 minutos)"
}
```

---

### 3. **Listar Reservas do Apartamento**

**GET** `/reservations?status=active&date_start=2030-03-01&date_end=2030-03-31`

Lista todas as reservas do apartamento (requer X-Apartment).

**Query Parameters:**
- `status`: `active` | `cancelled` | `all` (default: `active`)
- `date_start`: `YYYY-MM-DD` (default: hoje)
- `date_end`: `YYYY-MM-DD` (default: +30 dias)

**Response 200 OK:**
```json
{
  "apartment": "101",
  "reservations": [
    {
      "id": "RSV-4821",
      "area": "salao-de-festas",
      "date": "2030-03-10",
      "time_start": "14:00",
      "time_end": "18:00",
      "description": "Festa de aniversário",
      "status": "confirmed",
      "fee": 150.00
    },
    {
      "id": "RSV-4822",
      "area": "churrasqueira",
      "date": "2030-03-15",
      "time_start": "18:00",
      "time_end": "22:00",
      "description": null,
      "status": "pending_confirmation",
      "fee": null
    }
  ],
  "total": 2
}
```

---

### 4. **Consultar Regulamento**

**GET** `/regulation?keyword=taxa&limit=3`

Retorna trechos do regulamento (em até 3 resultados lexicais).

**Query Parameters:**
- `keyword`: palavra-chave para buscar (obrigatório)
- `limit`: máximo de trechos (default: 3, max: 3)

**Response 200 OK:**
```json
{
  "found": true,
  "keyword": "taxa",
  "excerpts": [
    {
      "order": 1,
      "text": "Art. 4º — Cobrança de taxa: A taxa é cobrada conforme a tabela do salão...",
      "relevance_score": 0.95
    },
    {
      "order": 2,
      "text": "Parágrafo 2º — Isenção de taxa: Moradores do condomínio não pagam taxa...",
      "relevance_score": 0.87
    }
  ]
}
```

**Response 200 (Não encontrado):**
```json
{
  "found": false,
  "keyword": "xyz",
  "excerpts": []
}
```

---

### 5. **Autorizar Visitante**

**POST** `/visitors/authorize`

Autoriza um visitante a entrar no condomínio (agente propõe, user confirma).

**Request Body:**
```json
{
  "visitor_name": "João da Silva",
  "visitor_rg": "12345678900",
  "apartment": "101"
}
```

**Response 200 OK:**
```json
{
  "visitor_id": "VIS-1001",
  "name": "João da Silva",
  "rg": "12345678900",
  "apartment": "101",
  "authorized": true,
  "authorized_at": "2026-10-07T15:30:00Z"
}
```

**Response 409 Conflict (Já autorizado):**
```json
{
  "error": "visitor_already_authorized",
  "message": "Este visitante já foi autorizado para este apartamento",
  "visitor_id": "VIS-1001"
}
```

---

### 6. **Recuperar Histórico de Eventos**

**GET** `/events?limit=10&offset=0`

Histórico de eventos da sessão atual (para resumo de contexto).

**Query Parameters:**
- `limit`: número de eventos (default: 10)
- `offset`: skip N eventos (default: 0)

**Response 200 OK:**
```json
{
  "session_id": "abc-123-def",
  "apartment": "101",
  "events": [
    {
      "id": 42,
      "type": "reserva_criada",
      "description": "Reserva RSV-4821 criada",
      "timestamp": "2026-10-07T14:00:00Z",
      "details": {
        "reservation_id": "RSV-4821",
        "area": "salao-de-festas"
      }
    },
    {
      "id": 43,
      "type": "reserva_confirmada",
      "description": "Reserva RSV-4821 confirmada",
      "timestamp": "2026-10-07T14:05:00Z",
      "details": {
        "reservation_id": "RSV-4821"
      }
    }
  ],
  "total": 42
}
```

---

## 📋 Esquemas Compartilhados

### Reservation (Objeto)

```json
{
  "id": "RSV-4821",
  "apartment": "101",
  "area": "salao-de-festas",
  "date": "2030-03-10",
  "time_start": "14:00",
  "time_end": "18:00",
  "description": "Festa de aniversário",
  "fee": 150.00,
  "confirmed": true,
  "active": true,
  "created_at": "2026-10-07T14:00:00Z",
  "updated_at": "2026-10-07T14:05:00Z"
}
```

### Visitor (Objeto)

```json
{
  "id": "VIS-1001",
  "apartment": "101",
  "name": "João da Silva",
  "rg": "12345678900",
  "authorized": true,
  "created_at": "2026-10-07T15:00:00Z"
}
```

### Confirmation (Objeto)

```json
{
  "id": "conf-1",
  "action": "reserva_criada",
  "description": "Confirmar criação de reserva no salão de festas?",
  "requires_response": true,
  "expires_at": "2026-10-07T14:05:00Z",
  "details": {
    "reservation_id": "RSV-4821",
    "area": "salao-de-festas"
  }
}
```

---

## 🔐 Segurança

| Aspecto | Implementação |
|--------|----------------|
| **Autenticação** | X-Apartment (valida owner via sessão) |
| **Isolamento** | Todos endpoints retornam apenas dados do apartamento da sessão |
| **Rate Limiting** | 30 req/min por session_id |
| **CORS** | Apenas origin do condomínio |
| **HTTPS** | Obrigatório em produção (TLS 1.2+) |

---

## ⏱️ Timeouts

| Operação | Timeout |
|----------|---------|
| POST /chat | 30s (inclui Gemini) |
| POST /confirmations | 5s |
| GET /reservations | 2s |
| GET /regulation | 2s |
| GET /events | 1s |

---

## 📖 Mais Informações

- **Implementação FastAPI:** [src/aurora/runtime/app.py](../../src/aurora/runtime/app.py)
- **Modelos Pydantic:** [src/aurora/runtime/schemas.py](../../src/aurora/runtime/schemas.py)
- **Testes de API:** [tests/integracao/test_api.py](../../tests/integracao/test_api.py)

---

**Última atualização:** 2026-10-07  
**Status:** ✅ Completo
