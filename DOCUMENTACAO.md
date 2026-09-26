# 📦 ROBOPOD — Documentação do MVP

## Bot de Delivery Noturno via WhatsApp (Arquitetura Híbrida LLM + Automação)

---

## 📋 Índice

1. [Visão Geral da Arquitetura](#1-visão-geral-da-arquitetura)
2. [Pré-requisitos](#2-pré-requisitos)
3. [Configuração do Ambiente](#3-configuração-do-ambiente)
4. [Variáveis de Ambiente](#4-variáveis-de-ambiente)
5. [Subindo o Projeto](#5-subindo-o-projeto)
6. [Playwright no WSL2](#6-playwright-no-wsl2)
7. [Ngrok — Expondo para a Internet](#7-ngrok--expondo-para-a-internet)
8. [Evolution API — Configuração do Webhook](#8-evolution-api--configuração-do-webhook)
9. [Fluxo da Máquina de Estados](#9-fluxo-da-máquina-de-estados)
10. [Estrutura do Projeto](#10-estrutura-do-projeto)
11. [API de Referência](#11-api-de-referência)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Visão Geral da Arquitetura

```
Cliente WhatsApp
     │
     ▼
Evolution API (Webhook POST)
     │
     ▼
┌────────────────────────────────┐
│   Django ASGI (views.py)       │
│   Roteamento por Estado        │
│                                │
│  ┌──────────────┐              │
│  │ LLM Service  │◄── OpenAI   │
│  │ (Guardrails) │    GPT-4o   │
│  └──────┬───────┘              │
│         │                      │
│    Function Calling            │
│    ┌────┴────┐                 │
│    │         │                 │
│  fechar   solicitar            │
│  pedido   humano               │
│    │         │                 │
│    ▼         ▼                 │
│  Scraper   Teams               │
│  (Uber)    Webhook             │
│    │                           │
│    ▼                           │
│  Cobrança PIX                  │
│  via WhatsApp                  │
└────────────────────────────────┘
```

**Fluxo Híbrido:**
- **Fase 1 (LLM):** A IA atua como garçom digital com guardrails rígidos.
- **Fase 2 (Determinística):** Django assume para cotação de frete e cobrança.
- **Escape (Humano):** Botão de pânico transfere para atendente.

---

## 2. Pré-requisitos

| Ferramenta      | Versão Mínima | Instalação                          |
|-----------------|---------------|-------------------------------------|
| WSL2            | Ubuntu 22.04+ | `wsl --install`                     |
| Docker Desktop  | 4.0+          | https://docker.com/desktop          |
| Docker Compose  | v2+           | Incluso no Docker Desktop           |
| Ngrok           | 3.0+          | `snap install ngrok` ou binário     |
| Python          | 3.11+         | Incluso na imagem Docker            |

---

## 3. Configuração do Ambiente

### 3.1. Clonar e configurar variáveis

```bash
# Clone o repositório
cd ~/projetos
git clone <url-do-repo> robopod
cd robopod

# Copie o arquivo de exemplo e preencha
cp .env.example .env
nano .env
```

### 3.2. Chave da OpenAI

1. Acesse https://platform.openai.com/api-keys
2. Crie uma nova API Key
3. Cole no `.env`:

```env
OPENAI_API_KEY=sk-proj-...
OPENAI_MODEL=gpt-4o-mini
```

> **💡 Dica:** O modelo `gpt-4o-mini` é o melhor custo-benefício para este MVP.
> Para testes locais baratos, use `gpt-4o-mini`. Para produção, considere `gpt-4o`.

### 3.3. Evolution API

1. Suba uma instância da Evolution API (Docker ou cloud)
2. Crie uma instância e conecte seu WhatsApp via QR Code
3. Preencha no `.env`:

```env
EVOLUTION_API_URL=http://localhost:8080
EVOLUTION_API_KEY=sua-chave-aqui
EVOLUTION_INSTANCE=robopod
```

### 3.4. Microsoft Teams Webhook

1. No Teams, vá no canal desejado → **Conectores** → **Incoming Webhook**
2. Nomeie como "Robopod Alertas" e copie a URL
3. Cole no `.env`:

```env
TEAMS_WEBHOOK_URL=https://outlook.office.com/webhook/...
```

### 3.5. Chave PIX

```env
CHAVE_PIX=seu-email@pix.com
# ou CPF, CNPJ, telefone, chave aleatória
```

---

## 4. Variáveis de Ambiente

| Variável              | Obrigatória | Descrição                                      |
|-----------------------|:-----------:|-------------------------------------------------|
| `DJANGO_SECRET_KEY`   | Sim         | Chave secreta do Django                         |
| `DEBUG`               | Não         | `True` para desenvolvimento (padrão: `True`)    |
| `OPENAI_API_KEY`      | Sim         | Chave da API da OpenAI                          |
| `OPENAI_MODEL`        | Não         | Modelo a usar (padrão: `gpt-4o-mini`)           |
| `EVOLUTION_API_URL`   | Sim         | URL base da Evolution API                       |
| `EVOLUTION_API_KEY`   | Sim         | Chave de autenticação da Evolution API          |
| `EVOLUTION_INSTANCE`  | Sim         | Nome da instância do WhatsApp                   |
| `ENDERECO_LOJA`       | Sim         | Endereço completo da loja (origem do frete)     |
| `TEAMS_WEBHOOK_URL`   | Não         | URL do Incoming Webhook do Teams                |
| `CHAVE_PIX`           | Sim         | Chave PIX para cobrança                         |
| `POSTGRES_DB`         | Não         | Nome do banco (padrão: `robopod`)               |
| `POSTGRES_USER`       | Não         | Usuário do banco (padrão: `robopod`)            |
| `POSTGRES_PASSWORD`   | Não         | Senha do banco (padrão: `robopod_secret`)       |

---

## 5. Subindo o Projeto

### 5.1. Com Docker Compose (recomendado)

```bash
# Build e sobe tudo
docker compose up --build -d

# Verifica logs
docker compose logs -f app

# Cria superusuário para o admin
docker compose exec app python manage.py createsuperuser
```

O app estará disponível em `http://localhost:8000`.  
O admin do Django em `http://localhost:8000/admin/`.

### 5.2. Sem Docker (desenvolvimento local)

```bash
# Crie e ative um virtualenv
python3.11 -m venv .venv
source .venv/bin/activate

# Instale dependências
pip install -r requirements.txt
playwright install chromium
playwright install-deps chromium

# Configure o banco (precisa de PostgreSQL rodando)
export POSTGRES_HOST=localhost
python manage.py migrate

# Rode o servidor ASGI
uvicorn robopod.asgi:application --host 0.0.0.0 --port 8000 --reload
```

---

## 6. Playwright no WSL2

O Playwright precisa de um servidor gráfico para rodar em modo `headless=False` (debug).

### 6.1. Instalar dependências do sistema

```bash
# Dentro do WSL2
sudo apt-get update
sudo apt-get install -y \
    libglib2.0-0 libnss3 libnspr4 libdbus-1-3 \
    libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
    libasound2 libatspi2.0-0 libwayland-client0
```

### 6.2. Modo headless=False (debug visual)

Para ver o navegador durante o debug, você precisa do **WSLg** (incluso no Windows 11)
ou instalar um servidor X como o **VcXsrv**:

```bash
# Windows 11 com WSLg — funciona automaticamente!
# Teste com:
playwright open https://m.uber.com

# Windows 10 — instale o VcXsrv e exporte o DISPLAY:
export DISPLAY=$(cat /etc/resolv.conf | grep nameserver | awk '{print $2}'):0
```

### 6.3. Modo headless=True (produção)

Para produção, altere em `core/scraper.py`:

```python
browser = await p.chromium.launch(headless=True)
```

---

## 7. Ngrok — Expondo para a Internet

A Evolution API precisa enviar webhooks para o seu servidor.
Em desenvolvimento local, use o Ngrok:

### 7.1. Instalar e autenticar

```bash
# Instalar
sudo snap install ngrok
# ou baixe de https://ngrok.com/download

# Autenticar (gratuito com conta)
ngrok config add-authtoken SEU_TOKEN
```

### 7.2. Iniciar o túnel

```bash
# Expõe a porta 8000
ngrok http 8000
```

Anote a URL gerada (ex: `https://abc123.ngrok-free.app`).

### 7.3. Configurar na Evolution API

A URL do webhook na Evolution API será:

```
https://abc123.ngrok-free.app/webhook/evolution/
```

> **⚠️ Atenção:** A URL do Ngrok muda a cada reinicialização (plano gratuito).
> Atualize o webhook na Evolution API sempre que reiniciar o Ngrok.

---

## 8. Evolution API — Configuração do Webhook

Na interface da Evolution API ou via cURL:

```bash
curl -X POST "${EVOLUTION_API_URL}/webhook/set/${EVOLUTION_INSTANCE}" \
  -H "Content-Type: application/json" \
  -H "apikey: ${EVOLUTION_API_KEY}" \
  -d '{
    "url": "https://SEU-NGROK.ngrok-free.app/webhook/evolution/",
    "webhook_by_events": false,
    "webhook_base64": false,
    "events": [
      "MESSAGES_UPSERT"
    ]
  }'
```

---

## 9. Fluxo da Máquina de Estados

```
     ┌───────────────────┐
     │  atendimento_llm  │◄──── Estado inicial
     └────────┬──────────┘
              │
    ┌─────────┼─────────────┐
    │         │             │
    ▼         ▼             ▼
 [texto]  [fechar_pedido]  [solicitar_humano]
    │         │             │
    │         ▼             ▼
    │  ┌──────────────┐  ┌──────────────────┐
    │  │ aguardando   │  │ atendimento      │
    │  │ _endereco    │  │ _humano          │
    │  └──────┬───────┘  └──────────────────┘
    │         │                  ▲
    │         ▼                  │ (falha)
    │  ┌──────────────┐          │
    │  │ calculando   │──────────┘
    │  │ _frete       │
    │  └──────┬───────┘
    │         │ (sucesso)
    │         ▼
    │  ┌──────────────────┐
    │  │ aguardando       │
    │  │ _comprovante     │
    │  └──────────────────┘
    │
    └──── (loop)
```

**Estados:**
| Estado                    | Descrição                                           |
|---------------------------|-----------------------------------------------------|
| `atendimento_llm`        | IA conversa com o cliente, oferece o cardápio        |
| `aguardando_endereco`    | Pedido fechado, aguardando endereço para frete       |
| `calculando_frete`       | Scraping do Uber em andamento (background task)      |
| `aguardando_comprovante` | Frete cotado, PIX enviado, aguardando pagamento      |
| `atendimento_humano`     | Conversa escalada para atendente humano              |

---

## 10. Estrutura do Projeto

```
robopod/
├── manage.py
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── .gitignore
│
├── robopod/                  # Configuração do projeto Django
│   ├── __init__.py
│   ├── asgi.py               # Ponto de entrada ASGI
│   ├── settings.py           # Configurações com variáveis de ambiente
│   └── urls.py               # Rotas raiz
│
└── core/                     # App principal do bot
    ├── __init__.py
    ├── apps.py
    ├── admin.py              # Admin para gerenciar sessões
    ├── models.py             # SessaoChat (máquina de estados)
    ├── urls.py               # Rota do webhook
    ├── views.py              # Webhook + roteamento + background tasks
    ├── llm_service.py        # Integração OpenAI com guardrails
    ├── scraper.py            # Cotação de frete via Playwright
    └── notificacoes.py       # WhatsApp (Evolution) e Teams
```

---

## 11. API de Referência

### Webhook Endpoint

```
POST /webhook/evolution/
Content-Type: application/json
```

**Payload esperado (Evolution API v2):**

```json
{
  "event": "messages.upsert",
  "data": {
    "key": {
      "remoteJid": "5511999999999@s.whatsapp.net",
      "fromMe": false
    },
    "message": {
      "conversation": "Quero um narguile"
    }
  }
}
```

**Resposta:** `200 OK` com `{"status": "ok"}`

### Admin Django

- URL: `http://localhost:8000/admin/`
- Visualize e gerencie sessões de chat
- Filtre por estado, busque por telefone

---

## 12. Troubleshooting

### Playwright não abre o navegador no WSL2

```bash
# Verifique se o WSLg está funcionando
echo $DISPLAY
# Deve mostrar algo como :0

# Reinstale os deps
playwright install-deps chromium
```

### Erro de conexão com o PostgreSQL

```bash
# Verifique se o container do banco está rodando
docker compose ps
docker compose logs db

# Teste a conexão
docker compose exec db psql -U robopod -d robopod -c "SELECT 1;"
```

### Webhook da Evolution não chega

1. Verifique se o Ngrok está rodando: `curl http://localhost:4040/api/tunnels`
2. Confira a URL cadastrada na Evolution API
3. Verifique os logs do Django: `docker compose logs -f app`
4. Teste manualmente:

```bash
curl -X POST http://localhost:8000/webhook/evolution/ \
  -H "Content-Type: application/json" \
  -d '{
    "event": "messages.upsert",
    "data": {
      "key": {"remoteJid": "5511999999999@s.whatsapp.net", "fromMe": false},
      "message": {"conversation": "Oi, quero fazer um pedido"}
    }
  }'
```

### OpenAI retorna erro 401

- Verifique se `OPENAI_API_KEY` está correta no `.env`
- Confirme que a chave não expirou em https://platform.openai.com/api-keys
- Verifique saldo/créditos na conta

### Frete não é cotado (scraper falha)

- A interface do Uber muda frequentemente — os seletores CSS podem quebrar
- Ajuste os seletores em `core/scraper.py` conforme necessário
- Para debug, rode com `headless=False` e observe o navegador
- Considere implementar um fallback com valor de frete fixo

---

## 📝 Notas de Desenvolvimento

- **Este é um MVP.** Em produção, considere:
  - Autenticação no webhook (verificar assinatura da Evolution API)
  - Rate limiting por telefone
  - Expiração de sessões inativas
  - Queue de tarefas (Celery/Dramatiq) ao invés de `asyncio.create_task`
  - Cache do menu em Redis
  - Monitoramento com Sentry
  - Testes automatizados

- **Sobre o scraping do Uber:** A interface do m.uber.com é dinâmica e pode mudar sem aviso. Mantenha os seletores atualizados e considere implementar fallbacks.

- **Custos estimados (OpenAI):** Com `gpt-4o-mini`, cada conversa completa custa aproximadamente US$ 0.01–0.03, dependendo do tamanho do histórico.
