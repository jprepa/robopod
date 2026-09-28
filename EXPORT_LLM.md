# Exportação do Projeto Robopod para LLM

Este documento contém a estrutura e todos os arquivos relevantes do projeto para facilitar a transição de contexto para outra LLM.

## Arquivo: `.env.example`

```example
# ── Django ──
DJANGO_SECRET_KEY=troque-por-uma-chave-segura-longa-aleatoria
DEBUG=False
ALLOWED_HOSTS=*

# ── PostgreSQL ──
POSTGRES_DB=robopod
POSTGRES_USER=robopod
POSTGRES_PASSWORD=TROQUE_POR_SENHA_FORTE
POSTGRES_HOST=db
POSTGRES_PORT=5432

# ── OpenAI ──
OPENAI_API_KEY=sk-proj-COLE_SUA_CHAVE_AQUI
OPENAI_MODEL=gpt-4o-mini

# ── Evolution API ──
EVOLUTION_API_URL=http://evolution:8080
EVOLUTION_API_KEY=minha-chave-evolution
EVOLUTION_INSTANCE=robopod

# ── Uber Scraping ──
ENDERECO_LOJA=Rua da Sua Loja, 123 - Bairro, Cidade - UF
PLAYWRIGHT_HEADLESS=true

# ── Microsoft Teams (opcional) ──
TEAMS_WEBHOOK_URL=

# ── PIX ──
CHAVE_PIX=sua-chave-pix-aqui

```

## Arquivo: `.gitignore`

```text
__pycache__/
*.py[cod]
*.egg-info/
dist/
build/
.env
db.sqlite3
staticfiles/
*.log
.venv/
venv/

```

## Arquivo: `docker-compose.yml`

```yml
services:
  # ── Banco de Dados ──
  db:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_DB: robopod
      POSTGRES_USER: robopod
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-robopod_secret}
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U robopod"]
      interval: 5s
      timeout: 5s
      retries: 5

  # ── Redis (necessário para Evolution API v2) ──
  redis:
    image: redis:7-alpine
    restart: unless-stopped
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 5s
      retries: 5

  # ── Evolution API (WhatsApp) ──
  evolution:
    image: evoapicloud/evolution-api:latest
    restart: unless-stopped
    ports:
      - "8083:8080"
    environment:
      AUTHENTICATION_API_KEY: ${EVOLUTION_API_KEY}
      DATABASE_PROVIDER: postgresql
      DATABASE_CONNECTION_URI: postgresql://robopod:${POSTGRES_PASSWORD:-robopod_secret}@db:5432/robopod?schema=evolution
      DATABASE_CONNECTION_CLIENT_NAME: evolution_robopod
      DATABASE_SAVE_DATA_INSTANCE: "true"
      DATABASE_SAVE_DATA_NEW_MESSAGE: "true"
      DATABASE_SAVE_MESSAGE_UPDATE: "true"
      DATABASE_SAVE_DATA_CONTACTS: "true"
      DATABASE_SAVE_DATA_CHATS: "true"
      CACHE_REDIS_ENABLED: "true"
      CACHE_REDIS_URI: redis://redis:6379
    volumes:
      - evolution_data:/evolution/instances
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_healthy

  # ── App Django (Bot) ──
  app:
    build: .
    restart: unless-stopped
    env_file:
      - .env
    ports:
      - "8000:8000"
    volumes:
      - .:/app
    depends_on:
      db:
        condition: service_healthy
      evolution:
        condition: service_started
    command: >
      sh -c "python manage.py migrate --noinput &&
             uvicorn robopod.asgi:application --host 0.0.0.0 --port 8000 --reload"

volumes:
  pgdata:
  redis_data:
  evolution_data:

```

## Arquivo: `Dockerfile`

```dockerfile
FROM python:3.11-slim

# Deps para o Playwright e PostgreSQL
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 libnss3 libnspr4 libdbus-1-3 libatk1.0-0 \
    libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libatspi2.0-0 libwayland-client0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instala navegadores do Playwright
RUN playwright install chromium
RUN playwright install-deps chromium

COPY . .

# Coleta arquivos estáticos (admin)
RUN python manage.py collectstatic --noinput 2>/dev/null || true

EXPOSE 8000

CMD ["uvicorn", "robopod.asgi:application", "--host", "0.0.0.0", "--port", "8000", "--reload"]

```

## Arquivo: `DOCUMENTACAO.md`

```md
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

```

## Arquivo: `GUIA_DEPLOY_VPS.md`

```md
# 🚀 Guia de Deploy — Robopod na VPS

> Guia adaptado para VPS que **já tem outro serviço rodando**.
> Cuidados extras para não conflitar portas nem derrubar o que já funciona.

---

## ⚠️ ANTES DE TUDO — Checagem da VPS

Acesse sua VPS via SSH e rode esses comandos para mapear o que já está ocupado:

```bash
# 1. Veja quais portas já estão em uso
ss -tlnp

# 2. Veja se já tem Docker instalado
docker --version
docker compose version

# 3. Veja se já tem Nginx rodando
systemctl status nginx

# 4. Veja a memória disponível
free -h
```

> **Anote as portas ocupadas!** O Robopod vai precisar de:
> - **8000** → Django (app do bot)
> - **8080** → Evolution API (WhatsApp)
> - **5432** → PostgreSQL
>
> Se alguma dessas já estiver em uso pelo seu outro SaaS, vamos trocar nas instruções abaixo.

---

## ETAPA 1 — Instalar Docker (se ainda não tiver)

Se o comando `docker --version` já retornou uma versão, **pule esta etapa**.

Caso contrário:

```bash
curl -fsSL https://get.docker.com | sh
apt install -y docker-compose-plugin
```

---

## ETAPA 2 — Criar uma pasta separada para o Robopod

Mantenha os projetos isolados. **Não misture com a pasta do outro SaaS.**

```bash
# Crie em um diretório próprio
mkdir -p /root/robopod
```

### Enviar os arquivos do seu PC para a VPS

No **PowerShell do Windows** (não na VPS):

```powershell
scp -r C:\Users\jppss\Desktop\robopod root@SEU_IP:/root/robopod
```

Ou se preferir Git:

```bash
# Na VPS
cd /root
git clone https://github.com/SEU_USER/robopod.git
```

---

## ETAPA 3 — Verificar e ajustar portas (IMPORTANTE!)

Acesse a pasta do projeto na VPS:

```bash
cd /root/robopod
```

Confira se as portas padrão estão livres:

```bash
# Testa cada porta
ss -tlnp | grep -E '8000|8080|5432'
```

### Se NENHUMA linha apareceu → Portas livres, siga para a Etapa 4.

### Se ALGUMA porta está ocupada → Ajuste o `docker-compose.yml`:

```bash
nano docker-compose.yml
```

Troque **somente a porta da ESQUERDA** (a do host). Exemplos:

| Porta padrão | Se estiver ocupada, troque para | Linha no docker-compose.yml |
|:---:|:---:|:---|
| `8000:8000` | `8001:8000` | No serviço `app` |
| `8080:8080` | `8081:8080` | No serviço `evolution` |
| `5432:5432` | `5433:5432` | No serviço `db` |

> **Exemplo:** Se seu outro SaaS já usa a porta 8080, mude a Evolution para 8081:
> ```yaml
>   evolution:
>     ports:
>       - "8081:8080"   # era "8080:8080"
> ```
>
> A porta da **direita** (interna do container) nunca muda.
> Se mexeu na porta do PostgreSQL externo, **não precisa mudar o `.env`** — o Django fala com o banco pela rede interna do Docker (porta 5432 sempre).

---

## ETAPA 4 — Configurar o .env

```bash
cp .env.example .env
nano .env
```

Preencha com seus dados reais:

```env
# ── Django ──
DJANGO_SECRET_KEY=cole-uma-chave-aleatoria-grande-aqui
DEBUG=False
ALLOWED_HOSTS=*

# ── PostgreSQL (não mexa se não mudou portas internas) ──
POSTGRES_DB=robopod
POSTGRES_USER=robopod
POSTGRES_PASSWORD=UmaSenhaForteAqui123!
POSTGRES_HOST=db
POSTGRES_PORT=5432

# ── OpenAI ──
OPENAI_API_KEY=sk-proj-SUA_CHAVE_AQUI
OPENAI_MODEL=gpt-4o-mini

# ── Evolution API (comunicação interna Docker, não mude) ──
EVOLUTION_API_URL=http://evolution:8080
EVOLUTION_API_KEY=minha-chave-evolution
EVOLUTION_INSTANCE=robopod

# ── Endereço da sua loja (origem do frete Uber) ──
ENDERECO_LOJA=Rua da Sua Loja, 123 - Bairro, Cidade - UF
PLAYWRIGHT_HEADLESS=true

# ── Teams (deixe vazio se não usar) ──
TEAMS_WEBHOOK_URL=

# ── PIX ──
CHAVE_PIX=sua-chave-pix
```

Para gerar a SECRET_KEY:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(50))"
```

Salvar: `Ctrl+O` → `Enter` → `Ctrl+X`

> **Sobre a `EVOLUTION_API_URL`:** Sempre `http://evolution:8080` — essa é a comunicação *interna* do Docker. Mesmo que você tenha trocado a porta externa para 8081, internamente continua 8080.

---

## ETAPA 5 — Subir os containers

```bash
cd /root/robopod
docker compose up --build -d
```

Primeira vez demora ~5 min (baixa imagens + instala Playwright/Chromium).

### Verifique se os 3 containers subiram:

```bash
docker compose ps
```

Saída esperada:

```
NAME                 STATUS
robopod-db-1         Up (healthy)
robopod-evolution-1  Up
robopod-app-1        Up
```

### Se algum container caiu:

```bash
# Veja o erro
docker compose logs app
docker compose logs evolution
docker compose logs db
```

### Crie o superusuário do painel admin:

```bash
docker compose exec app python manage.py createsuperuser
```

### Confirme que o Django está respondendo:

```bash
# Use a porta que você configurou (8000 ou a que trocou)
curl -s http://localhost:8000/admin/ | head -5
```

Se retornar HTML, está vivo. ✅

---

## ETAPA 6 — Conectar o WhatsApp

> Nas instruções abaixo, substitua `8080` pela porta que você usou se trocou (ex: `8081`).

### 6.1 Libere a porta no firewall (se usa ufw):

```bash
# Só libere se precisa acessar de fora (para escanear QR Code no navegador)
ufw allow 8080/tcp   # ou 8081 se trocou
ufw allow 8000/tcp   # ou 8001 se trocou
```

### 6.2 Crie a instância do WhatsApp:

```bash
curl -X POST "http://localhost:8080/instance/create" \
  -H "Content-Type: application/json" \
  -H "apikey: minha-chave-evolution" \
  -d '{
    "instanceName": "robopod",
    "integration": "WHATSAPP-BAILEYS",
    "qrcode": true
  }'
```

### 6.3 Escaneie o QR Code:

Abra no navegador do seu PC:

```
http://SEU_IP_DA_VPS:8080/manager
```

*(Use a apikey `minha-chave-evolution` se pedir login.)*

Clique na instância **robopod** → escaneie o QR Code com o **WhatsApp do chip dedicado** (no celular: Aparelhos Conectados → Conectar um aparelho).

### 6.4 Confirme a conexão:

```bash
curl -s "http://localhost:8080/instance/connectionState/robopod" \
  -H "apikey: minha-chave-evolution"
```

Resposta esperada: `"state": "open"` ✅

---

## ETAPA 7 — Configurar o Webhook (Evolution → Django)

```bash
curl -X POST "http://localhost:8080/webhook/set/robopod" \
  -H "Content-Type: application/json" \
  -H "apikey: minha-chave-evolution" \
  -d '{
    "url": "http://app:8000/webhook/evolution/",
    "webhook_by_events": false,
    "webhook_base64": false,
    "events": [
      "MESSAGES_UPSERT"
    ]
  }'
```

> A URL é `http://app:8000` porque dentro da rede Docker os containers se enxergam pelo nome do serviço. **Não precisa trocar** mesmo que tenha mudado a porta externa.

Confirme:

```bash
curl -s "http://localhost:8080/webhook/find/robopod" \
  -H "apikey: minha-chave-evolution"
```

---

## ETAPA 8 — Testar no WhatsApp! 🎉

### 8.1 Abra o log ao vivo:

```bash
docker compose logs -f app
```

### 8.2 Mande uma mensagem do seu celular pessoal para o número do bot:

> "Boa noite! O que vocês têm no cardápio?"

### 8.3 O que deve acontecer:

1. No terminal, você vê a mensagem chegar.
2. A OpenAI processa com os guardrails.
3. O bot responde pelo WhatsApp com o cardápio.

### 8.4 Se NÃO funcionou — teste parte por parte:

```bash
# 1. O Django está vivo?
curl -X POST http://localhost:8000/webhook/evolution/ \
  -H "Content-Type: application/json" \
  -d '{
    "event": "messages.upsert",
    "data": {
      "key": {
        "remoteJid": "5511999999999@s.whatsapp.net",
        "fromMe": false
      },
      "message": {
        "conversation": "Oi, quero fazer um pedido"
      }
    }
  }'
# Deve retornar: {"status": "ok"}

# 2. A Evolution está conectada?
curl -s "http://localhost:8080/instance/connectionState/robopod" \
  -H "apikey: minha-chave-evolution"
# Deve retornar: "state": "open"

# 3. O webhook está configurado?
curl -s "http://localhost:8080/webhook/find/robopod" \
  -H "apikey: minha-chave-evolution"
# Deve mostrar a URL http://app:8000/webhook/evolution/
```

---

## ETAPA 9 — (Opcional) Nginx com seu domínio existente

Se você já tem Nginx rodando para o outro SaaS, **NÃO recrie a config inteira**. Adicione um novo server block:

### 9.1 Aponte um subdomínio para a mesma VPS:

No DNS do seu domínio, adicione:

```
Tipo: A
Nome: bot
Valor: IP_DA_VPS
```

Resultado: `bot.seudominio.com.br` → IP da VPS

### 9.2 Crie APENAS o arquivo do Robopod (não mexa nos outros):

```bash
nano /etc/nginx/sites-available/robopod
```

Cole:

```nginx
server {
    listen 80;
    server_name bot.seudominio.com.br;

    location /webhook/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /admin/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location /static/ {
        proxy_pass http://127.0.0.1:8000;
    }
}
```

### 9.3 Ative e teste (SEM reiniciar — só reload):

```bash
ln -s /etc/nginx/sites-available/robopod /etc/nginx/sites-enabled/

# TESTE antes de aplicar (não derruba o outro site se tiver erro)
nginx -t

# Se mostrou "syntax is ok" e "test is successful":
systemctl reload nginx
```

> **`reload` ≠ `restart`.** O reload aplica a nova config sem derrubar as conexões existentes do seu outro SaaS.

### 9.4 SSL com Let's Encrypt:

```bash
certbot --nginx -d bot.seudominio.com.br
```

---

## 🛠️ Comandos do Dia a Dia

```bash
cd /root/robopod

# Status dos containers
docker compose ps

# Logs ao vivo (só do bot)
docker compose logs -f app

# Reiniciar só o bot (sem mexer no banco/evolution)
docker compose restart app

# Atualizar código
git pull && docker compose up --build -d

# Parar TUDO do Robopod (não afeta seu outro SaaS)
docker compose down

# Ver sessões no banco
docker compose exec app python manage.py shell -c "
from core.models import SessaoChat
for s in SessaoChat.objects.all():
    print(f'{s.telefone} | {s.estado_atual} | {s.ultima_interacao}')
"

# Resetar sessão de um cliente travado
docker compose exec app python manage.py shell -c "
from core.models import SessaoChat
s = SessaoChat.objects.get(telefone='5511999999999')
s.resetar_sessao()
print('Resetada!')
"

# Admin do Django
# http://SEU_IP:8000/admin/
```

---

## 🔥 Troubleshooting Rápido

| Problema | Comando para investigar | Solução |
|----------|------------------------|---------|
| Container não sobe | `docker compose logs app` | Leia o erro no log |
| Conflito de porta | `ss -tlnp \| grep PORTA` | Troque a porta no docker-compose.yml |
| WhatsApp desconectou | `curl .../connectionState/robopod` | Reconecte pelo manager :8080 |
| Bot não responde | `docker compose logs -f app` | Verifique OPENAI_API_KEY no .env |
| Erro 401 OpenAI | Chave inválida | Gere nova em platform.openai.com |
| Frete não calcula | Log: "Timeout no scraping" | Confirme PLAYWRIGHT_HEADLESS=true |
| Nginx deu erro | `nginx -t` | Corrija a syntax antes do reload |
| Outro SaaS caiu | `docker compose ps` na pasta do outro | Os projetos são isolados, verifique logs |
| Sem memória | `free -h` e `docker stats` | Mín. 2GB RAM para Playwright |

```

## Arquivo: `manage.py`

```python
#!/usr/bin/env python
import os
import sys

def main():
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'robopod.settings')
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)

if __name__ == '__main__':
    main()

```

## Arquivo: `requirements.txt`

```txt
Django>=5.0,<6.0
psycopg[binary]>=3.1
uvicorn[standard]>=0.30
httpx>=0.27
openai>=1.30
python-dotenv>=1.0
playwright>=1.44

```

## Arquivo: `core/admin.py`

```python
from django.contrib import admin
from .models import SessaoChat


@admin.register(SessaoChat)
class SessaoChatAdmin(admin.ModelAdmin):
    list_display = ('telefone', 'estado_atual', 'ultima_interacao')
    list_filter = ('estado_atual',)
    search_fields = ('telefone',)
    readonly_fields = ('criado_em', 'ultima_interacao')

```

## Arquivo: `core/apps.py`

```python
from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'
    verbose_name = 'Core - Bot Delivery'

```

## Arquivo: `core/llm_service.py`

```python
"""Serviço de integração com a API da OpenAI.

Implementa o fluxo conversacional com guardrails estritos e
Function Calling para fechar pedidos ou escalar para humano.
"""

import json
import logging
from typing import Any

from django.conf import settings
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

# ── Cliente OpenAI (singleton por módulo) ──
_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    return _client


# ── Catálogo estruturado da Tabacaria ──
CATALOGO: dict[str, Any] = {
    'categorias': [
        {
            'id': 'narguiles',
            'titulo': '🔥 Narguilés',
            'itens': [
                {
                    'id': 'sessao_robopod',
                    'nome': 'Sessão Robopod (completa)',
                    'preco': 89.90,
                    'descricao': 'Narguilé preparado e completo para sua sessão',
                    'imagem_url': '',
                    'variantes': [],
                },
            ],
        },
        {
            'id': 'essencias',
            'titulo': '🍬 Essências',
            'itens': [
                {
                    'id': 'essencia_premium',
                    'nome': 'Essência Premium (50g)',
                    'preco': 25.00,
                    'descricao': 'Essência premium de alta qualidade',
                    'imagem_url': '',
                    'variantes': [
                        {'id': 'ep_menta', 'nome': 'Menta', 'imagem_url': ''},
                        {'id': 'ep_morango', 'nome': 'Morango', 'imagem_url': ''},
                        {'id': 'ep_uva', 'nome': 'Uva', 'imagem_url': ''},
                        {'id': 'ep_tutti_frutti', 'nome': 'Tutti Frutti', 'imagem_url': ''},
                    ],
                },
                {
                    'id': 'essencia_gold',
                    'nome': 'Essência Gold (50g)',
                    'preco': 35.00,
                    'descricao': 'Essência gold linha especial',
                    'imagem_url': '',
                    'variantes': [
                        {'id': 'eg_love66', 'nome': 'Love 66', 'imagem_url': ''},
                        {'id': 'eg_double_apple', 'nome': 'Double Apple', 'imagem_url': ''},
                        {'id': 'eg_blueberry', 'nome': 'Blueberry Mint', 'imagem_url': ''},
                    ],
                },
            ],
        },
        {
            'id': 'carvao',
            'titulo': '🪨 Carvão',
            'itens': [
                {
                    'id': 'carvao_hexagonal',
                    'nome': 'Carvão Hexagonal (caixa 250g)',
                    'preco': 15.00,
                    'descricao': 'Carvão hexagonal de coco 250g',
                    'imagem_url': '',
                    'variantes': [],
                },
                {
                    'id': 'carvao_coco',
                    'nome': 'Carvão Coco (caixa 500g)',
                    'preco': 28.00,
                    'descricao': 'Carvão de coco premium 500g',
                    'imagem_url': '',
                    'variantes': [],
                },
            ],
        },
        {
            'id': 'acessorios',
            'titulo': '🧴 Acessórios',
            'itens': [
                {
                    'id': 'piteira',
                    'nome': 'Piteira descartável (10 un.)',
                    'preco': 8.00,
                    'descricao': 'Pacote com 10 piteiras descartáveis',
                    'imagem_url': '',
                    'variantes': [],
                },
                {
                    'id': 'mangueira',
                    'nome': 'Mangueira lavável',
                    'preco': 22.00,
                    'descricao': 'Mangueira de silicone lavável',
                    'imagem_url': '',
                    'variantes': [
                        {'id': 'mg_preta', 'nome': 'Preta', 'imagem_url': ''},
                        {'id': 'mg_transparente', 'nome': 'Transparente', 'imagem_url': ''},
                        {'id': 'mg_azul', 'nome': 'Azul', 'imagem_url': ''},
                    ],
                },
                {
                    'id': 'base_vaso',
                    'nome': 'Base / vaso de reposição',
                    'preco': 45.00,
                    'descricao': 'Vaso de vidro para reposição',
                    'imagem_url': '',
                    'variantes': [],
                },
                {
                    'id': 'rosh',
                    'nome': 'Rosh / Cabeça de cerâmica',
                    'preco': 30.00,
                    'descricao': 'Rosh de cerâmica artesanal',
                    'imagem_url': '',
                    'variantes': [],
                },
            ],
        },
        {
            'id': 'bebidas',
            'titulo': '🥤 Bebidas',
            'itens': [
                {
                    'id': 'agua',
                    'nome': 'Água (500ml)',
                    'preco': 5.00,
                    'descricao': 'Água mineral 500ml',
                    'imagem_url': '',
                    'variantes': [],
                },
                {
                    'id': 'refrigerante',
                    'nome': 'Refrigerante lata (350ml)',
                    'preco': 7.00,
                    'descricao': 'Refrigerante lata 350ml',
                    'imagem_url': '',
                    'variantes': [
                        {'id': 'ref_coca', 'nome': 'Coca-Cola', 'imagem_url': ''},
                        {'id': 'ref_guarana', 'nome': 'Guaraná', 'imagem_url': ''},
                        {'id': 'ref_sprite', 'nome': 'Sprite', 'imagem_url': ''},
                    ],
                },
                {
                    'id': 'suco',
                    'nome': 'Suco natural (300ml)',
                    'preco': 10.00,
                    'descricao': 'Suco natural da fruta 300ml',
                    'imagem_url': '',
                    'variantes': [
                        {'id': 'sc_laranja', 'nome': 'Laranja', 'imagem_url': ''},
                        {'id': 'sc_maracuja', 'nome': 'Maracujá', 'imagem_url': ''},
                    ],
                },
                {
                    'id': 'energetico',
                    'nome': 'Energético (250ml)',
                    'preco': 12.00,
                    'descricao': 'Energético lata 250ml',
                    'imagem_url': '',
                    'variantes': [],
                },
            ],
        },
    ],
}


# ── Funções auxiliares do catálogo ──

def gerar_menu_texto() -> str:
    """Gera o cardápio em formato texto para o system prompt da IA.

    Percorre o CATALOGO estruturado e monta uma string legível
    no mesmo formato do antigo MENU_TABACARIA estático.
    """
    linhas: list[str] = ['📋 CARDÁPIO ROBOPOD — DELIVERY NOTURNO', '']

    for categoria in CATALOGO['categorias']:
        # Título da categoria (ex.: "🔥 NARGUILÉS")
        titulo_upper = categoria['titulo'].split(' ', 1)
        emoji = titulo_upper[0]
        nome_cat = titulo_upper[1].upper() if len(titulo_upper) > 1 else ''
        linhas.append(f'{emoji} {nome_cat}')

        for item in categoria['itens']:
            preco_fmt = f"R$ {item['preco']:,.2f}".replace('.', '_').replace(',', '.').replace('_', ',')
            linhas.append(f"  • {item['nome']:<35s} — {preco_fmt}")

            # Lista variantes disponíveis, se houver
            if item['variantes']:
                nomes_var = [v['nome'] for v in item['variantes']]
                linhas.append(f"    Sabores/opções: {', '.join(nomes_var)}")

        linhas.append('')  # linha em branco entre categorias

    return '\n'.join(linhas)


def gerar_secoes_lista() -> list[dict]:
    """Gera as seções para a mensagem de lista interativa do WhatsApp (sendList).

    Cada categoria vira uma seção; cada item vira uma row com
    rowId = item['id'], title = item['nome'] e description = preço formatado.

    Returns:
        Lista de seções no formato da Evolution API.
    """
    secoes: list[dict] = []

    for categoria in CATALOGO['categorias']:
        rows: list[dict] = []
        for item in categoria['itens']:
            rows.append({
                'rowId': item['id'],
                'title': item['nome'],
                'description': f"R$ {item['preco']:.2f}",
            })
        secoes.append({
            'title': categoria['titulo'],
            'rows': rows,
        })

    return secoes


def gerar_secoes_variantes(item_id: str) -> list[dict] | None:
    """Gera seções de variantes para um item específico (sendList).

    Localiza o item pelo ID em todas as categorias. Se possuir variantes,
    retorna uma lista de seções pronta para o formato Evolution API.

    Returns:
        Lista de seções com as variantes, ou None se o item não tem variantes.
    """
    item = buscar_item(item_id)
    if item is None or not item['variantes']:
        return None

    rows: list[dict] = []
    for variante in item['variantes']:
        rows.append({
            'rowId': variante['id'],
            'title': variante['nome'],
        })

    return [{'title': f"Opções — {item['nome']}", 'rows': rows}]


def buscar_item(item_id: str) -> dict | None:
    """Busca um item no catálogo pelo ID."""
    for categoria in CATALOGO['categorias']:
        for item in categoria['itens']:
            if item['id'] == item_id:
                return item
    return None


def buscar_variante(item_id: str, variante_id: str) -> dict | None:
    """Busca uma variante específica de um item."""
    item = buscar_item(item_id)
    if item is None:
        return None
    for variante in item['variantes']:
        if variante['id'] == variante_id:
            return variante
    return None


def item_tem_variantes(item_id: str) -> bool:
    """Verifica se um item possui variantes (sabores/cores)."""
    item = buscar_item(item_id)
    if item is None:
        return False
    return len(item['variantes']) > 0


# ── Menu da Tabacaria (gerado dinamicamente a partir do catálogo) ──
MENU_TABACARIA = gerar_menu_texto()

SYSTEM_PROMPT = f"""Você é o atendente virtual da Robopod, uma tabacaria delivery noturna.

REGRAS INVIOLÁVEIS:
1. Você SÓ pode oferecer os itens listados no CARDÁPIO abaixo. Se o cliente pedir algo que não existe no cardápio, diga educadamente que não está disponível.
2. NUNCA invente produtos, preços ou promoções.
3. NUNCA ofereça descontos ou altere preços.
4. Respostas devem ser CURTAS e objetivas (máximo 3 frases por mensagem).
5. Seja simpático e use emojis com moderação.
6. Se o cliente perguntar sobre algo fora do escopo da tabacaria (política, saúde, etc.), redirecione educadamente para o cardápio.
7. Se o cliente pedir para falar com um humano, ou se você não souber responder, use IMEDIATAMENTE a ferramenta `solicitar_humano`.
8. Quando o cliente confirmar que quer finalizar o pedido, use a ferramenta `fechar_pedido` com os itens e quantidades corretos.
9. Sempre confirme o pedido com o cliente ANTES de chamar `fechar_pedido`.
10. Horário de funcionamento: 18h às 03h. Fora desse horário, informe que estamos fechados.

CARDÁPIO:
{MENU_TABACARIA}
"""

# ── Definição das Tools (Function Calling) ──
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "fechar_pedido",
            "description": (
                "Chamada quando o cliente CONFIRMA a compra. "
                "Gera o carrinho com os itens e quantidades do pedido."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "itens": {
                        "type": "array",
                        "description": "Lista de itens do pedido",
                        "items": {
                            "type": "object",
                            "properties": {
                                "nome": {
                                    "type": "string",
                                    "description": "Nome do item exatamente como no cardápio",
                                },
                                "quantidade": {
                                    "type": "integer",
                                    "description": "Quantidade do item",
                                    "minimum": 1,
                                },
                                "preco_unitario": {
                                    "type": "number",
                                    "description": "Preço unitário do item em reais",
                                },
                            },
                            "required": ["nome", "quantidade", "preco_unitario"],
                        },
                    },
                },
                "required": ["itens"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "solicitar_humano",
            "description": (
                "Botão de pânico. Chamada quando: "
                "(a) o cliente pede para falar com um atendente humano, "
                "(b) a IA não sabe responder, "
                "(c) o assunto foge do escopo da tabacaria."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "motivo": {
                        "type": "string",
                        "description": "Breve motivo da transferência para humano",
                    },
                },
                "required": ["motivo"],
            },
        },
    },
]


# ── Tipos de retorno ──
class LLMResult:
    """Resultado do processamento da LLM."""

    def __init__(
        self,
        tipo: str,  # 'texto' | 'fechar_pedido' | 'solicitar_humano'
        conteudo: str = '',
        dados: dict[str, Any] | None = None,
    ):
        self.tipo = tipo
        self.conteudo = conteudo
        self.dados = dados or {}

    def __repr__(self):
        return f'LLMResult(tipo={self.tipo!r}, conteudo={self.conteudo[:50]!r})'


async def processar_mensagem(
    historico: list[dict[str, str]],
    mensagem_usuario: str,
) -> tuple[LLMResult, list[dict[str, str]]]:
    """Processa uma mensagem do usuário via OpenAI Chat Completions.

    Args:
        historico: Lista de mensagens anteriores (formato OpenAI).
        mensagem_usuario: Texto enviado pelo cliente.

    Returns:
        Tupla (resultado, historico_atualizado).
    """
    client = _get_client()

    # Monta mensagens com system prompt + histórico + nova mensagem
    mensagens = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *historico,
        {"role": "user", "content": mensagem_usuario},
    ]

    try:
        response = await client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=mensagens,
            tools=TOOLS,
            tool_choice="auto",
            temperature=0.3,
            max_tokens=500,
        )
    except Exception:
        logger.exception('Erro ao chamar OpenAI API')
        return (
            LLMResult(
                tipo='texto',
                conteudo='😕 Desculpe, estou com um problema técnico. Tente novamente em instantes.',
            ),
            historico,
        )

    choice = response.choices[0]
    message = choice.message

    # Atualiza histórico
    novo_historico = [
        *historico,
        {"role": "user", "content": mensagem_usuario},
    ]

    # ── Verifica se houve Function Calling ──
    if message.tool_calls:
        tool_call = message.tool_calls[0]
        nome_funcao = tool_call.function.name
        argumentos = json.loads(tool_call.function.arguments)

        logger.info('Function call: %s -> %s', nome_funcao, argumentos)

        # Adiciona a mensagem do assistente com tool_calls ao histórico
        novo_historico.append(message.model_dump(exclude_none=True))

        if nome_funcao == 'fechar_pedido':
            return (
                LLMResult(
                    tipo='fechar_pedido',
                    conteudo='Pedido fechado com sucesso!',
                    dados=argumentos,
                ),
                novo_historico,
            )

        if nome_funcao == 'solicitar_humano':
            return (
                LLMResult(
                    tipo='solicitar_humano',
                    conteudo='Transferindo para atendente humano.',
                    dados=argumentos,
                ),
                novo_historico,
            )

    # ── Resposta de texto simples ──
    texto_resposta = message.content or ''
    novo_historico.append({"role": "assistant", "content": texto_resposta})

    return (
        LLMResult(tipo='texto', conteudo=texto_resposta),
        novo_historico,
    )

```

## Arquivo: `core/models.py`

```python
from django.db import models
from django.utils import timezone


class SessaoChat(models.Model):
    """Sessão de conversa com um cliente via WhatsApp.

    Cada número de telefone possui uma única sessão ativa.
    O campo `estado_atual` controla a máquina de estados do fluxo híbrido.
    """

    class Estado(models.TextChoices):
        ATENDIMENTO_LLM = 'atendimento_llm', 'Atendimento LLM'
        AGUARDANDO_ENDERECO = 'aguardando_endereco', 'Aguardando Endereço'
        CALCULANDO_FRETE = 'calculando_frete', 'Calculando Frete'
        AGUARDANDO_COMPROVANTE = 'aguardando_comprovante', 'Aguardando Comprovante'
        ATENDIMENTO_HUMANO = 'atendimento_humano', 'Atendimento Humano'

    telefone = models.CharField(
        max_length=20,
        unique=True,
        db_index=True,
        help_text='Número do WhatsApp no formato 55XXXXXXXXXXX',
    )
    estado_atual = models.CharField(
        max_length=30,
        choices=Estado.choices,
        default=Estado.ATENDIMENTO_LLM,
    )
    carrinho_temporario = models.JSONField(
        default=dict,
        blank=True,
        help_text='Carrinho gerado pela function calling da LLM: {"itens": [...]}',
    )
    historico_mensagens = models.JSONField(
        default=list,
        blank=True,
        help_text='Histórico completo de mensagens para contexto da OpenAI',
    )
    ultima_interacao = models.DateTimeField(
        default=timezone.now,
        db_index=True,
    )
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Sessão de Chat'
        verbose_name_plural = 'Sessões de Chat'
        ordering = ['-ultima_interacao']

    def __str__(self):
        return f'{self.telefone} [{self.get_estado_atual_display()}]'

    def resetar_sessao(self):
        """Reseta a sessão para o estado inicial (novo atendimento)."""
        self.estado_atual = self.Estado.ATENDIMENTO_LLM
        self.carrinho_temporario = {}
        self.historico_mensagens = []
        self.ultima_interacao = timezone.now()
        self.save(update_fields=[
            'estado_atual', 'carrinho_temporario',
            'historico_mensagens', 'ultima_interacao',
        ])

```

## Arquivo: `core/notificacoes.py`

```python
"""Serviço de notificações externas (Evolution API e Microsoft Teams)."""

import logging

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)


async def enviar_whatsapp(telefone: str, mensagem: str) -> bool:
    """Envia mensagem de texto via Evolution API.

    Args:
        telefone: Número no formato 55XXXXXXXXXXX.
        mensagem: Texto da mensagem.

    Returns:
        True se o envio foi bem-sucedido.
    """
    url = (
        f"{settings.EVOLUTION_API_URL}/message/sendText/"
        f"{settings.EVOLUTION_INSTANCE}"
    )
    headers = {
        'Content-Type': 'application/json',
        'apikey': settings.EVOLUTION_API_KEY,
    }
    payload = {
        'number': telefone,
        'text': mensagem,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            logger.info('WhatsApp enviado para %s', telefone)
            return True
    except Exception:
        logger.exception('Falha ao enviar WhatsApp para %s', telefone)
        return False


async def enviar_imagem_whatsapp(
    telefone: str,
    url_imagem: str,
    legenda: str = '',
) -> bool:
    """Envia imagem com legenda opcional via Evolution API.

    Args:
        telefone: Número no formato 55XXXXXXXXXXX.
        url_imagem: URL pública da imagem.
        legenda: Legenda opcional para a imagem.

    Returns:
        True se o envio foi bem-sucedido.
    """
    url = (
        f"{settings.EVOLUTION_API_URL}/message/sendMedia/"
        f"{settings.EVOLUTION_INSTANCE}"
    )
    headers = {
        'Content-Type': 'application/json',
        'apikey': settings.EVOLUTION_API_KEY,
    }
    payload = {
        'number': telefone,
        'mediatype': 'image',
        'mimetype': 'image/jpeg',
        'caption': legenda,
        'media': url_imagem,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            logger.info('Imagem WhatsApp enviada para %s', telefone)
            return True
    except Exception:
        logger.exception('Falha ao enviar imagem WhatsApp para %s', telefone)
        return False


async def enviar_lista_whatsapp(
    telefone: str,
    titulo: str,
    descricao: str,
    botao_texto: str,
    secoes: list[dict],
    rodape: str = '',
) -> bool:
    """Envia mensagem interativa do tipo lista via Evolution API.

    Args:
        telefone: Número no formato 55XXXXXXXXXXX.
        titulo: Título da mensagem.
        descricao: Descrição/corpo da mensagem.
        botao_texto: Texto do botão principal para abrir a lista.
        secoes: Lista de seções contendo título e opções ('title' e 'rows').
        rodape: Texto de rodapé opcional.

    Returns:
        True se o envio foi bem-sucedido.
    """
    url = (
        f"{settings.EVOLUTION_API_URL}/message/sendList/"
        f"{settings.EVOLUTION_INSTANCE}"
    )
    headers = {
        'Content-Type': 'application/json',
        'apikey': settings.EVOLUTION_API_KEY,
    }
    payload = {
        'number': telefone,
        'title': titulo,
        'description': descricao,
        'buttonText': botao_texto,
        'footerText': rodape,
        'sections': secoes,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            logger.info('Lista interativa WhatsApp enviada para %s', telefone)
            return True
    except Exception:
        logger.exception('Falha ao enviar lista WhatsApp para %s', telefone)
        return False


async def enviar_botoes_whatsapp(
    telefone: str,
    titulo: str,
    descricao: str,
    botoes: list[dict],
    rodape: str = '',
) -> bool:
    """Envia mensagem interativa com botões de resposta via Evolution API.

    Args:
        telefone: Número no formato 55XXXXXXXXXXX.
        titulo: Título da mensagem.
        descricao: Descrição/corpo da mensagem.
        botoes: Lista de botões com 'type', 'displayText' e 'id'.
        rodape: Texto de rodapé opcional.

    Returns:
        True se o envio foi bem-sucedido.
    """
    url = (
        f"{settings.EVOLUTION_API_URL}/message/sendButtons/"
        f"{settings.EVOLUTION_INSTANCE}"
    )
    headers = {
        'Content-Type': 'application/json',
        'apikey': settings.EVOLUTION_API_KEY,
    }
    payload = {
        'number': telefone,
        'title': titulo,
        'description': descricao,
        'footer': rodape,
        'buttons': botoes,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            logger.info('Botões WhatsApp enviados para %s', telefone)
            return True
    except Exception:
        logger.exception('Falha ao enviar botões WhatsApp para %s', telefone)
        return False


async def notificar_teams(mensagem: str) -> bool:
    """Envia notificação para o canal do Microsoft Teams via Incoming Webhook.

    Args:
        mensagem: Texto da notificação.

    Returns:
        True se o envio foi bem-sucedido.
    """
    webhook_url = settings.TEAMS_WEBHOOK_URL
    if not webhook_url:
        logger.warning('TEAMS_WEBHOOK_URL não configurada, notificação ignorada.')
        return False

    payload = {
        'text': mensagem,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(webhook_url, json=payload)
            resp.raise_for_status()
            logger.info('Notificação Teams enviada')
            return True
    except Exception:
        logger.exception('Falha ao notificar Teams')
        return False

```

## Arquivo: `core/scraper.py`

```python
"""Scraper assíncrono para cotação de frete via Uber (m.uber.com).

Utiliza Playwright para preencher endereços e raspar o valor
da opção "Envios Moto" na interface mobile da Uber.
"""

import logging
import os
import re

from django.conf import settings
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# Em VPS sem display, DEVE ser True. Para debug local, setar PLAYWRIGHT_HEADLESS=false
HEADLESS = os.getenv('PLAYWRIGHT_HEADLESS', 'true').lower() in ('true', '1', 'yes')

logger = logging.getLogger(__name__)

UBER_URL = 'https://m.uber.com/go/delivery'


async def cotar_frete_uber(endereco_cliente: str) -> dict:
    """Cota o frete no Uber via scraping de m.uber.com.

    Args:
        endereco_cliente: Endereço completo do cliente (rua, número, bairro, cidade).

    Returns:
        dict com:
            - sucesso (bool)
            - valor_frete (float | None): valor em reais
            - mensagem (str): mensagem de status ou erro
    """
    endereco_loja = settings.ENDERECO_LOJA

    logger.info(
        'Iniciando cotação Uber: %s -> %s',
        endereco_loja,
        endereco_cliente,
    )

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=HEADLESS)
            context = await browser.new_context(
                locale='pt-BR',
                viewport={'width': 375, 'height': 812},
                user_agent=(
                    'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) '
                    'AppleWebKit/605.1.15 (KHTML, like Gecko) '
                    'Version/16.0 Mobile/15E148 Safari/604.1'
                ),
            )
            page = await context.new_page()

            # ── Acessa a página de delivery ──
            await page.goto(UBER_URL, wait_until='networkidle', timeout=30_000)
            await page.wait_for_timeout(2000)

            # ── Preenche endereço de ORIGEM (loja) ──
            pickup_input = page.locator(
                'input[placeholder*="Retirada"], '
                'input[placeholder*="Pickup"], '
                'input[data-testid="pickup-input"]'
            ).first
            await pickup_input.click()
            await pickup_input.fill(endereco_loja)
            await page.wait_for_timeout(1500)

            # Seleciona primeira sugestão de endereço
            primeira_sugestao = page.locator(
                '[data-testid="location-suggestion"], '
                'li[role="option"], '
                'div[class*="suggestion"]'
            ).first
            await primeira_sugestao.click()
            await page.wait_for_timeout(1000)

            # ── Preenche endereço de DESTINO (cliente) ──
            dropoff_input = page.locator(
                'input[placeholder*="Entrega"], '
                'input[placeholder*="Dropoff"], '
                'input[data-testid="dropoff-input"]'
            ).first
            await dropoff_input.click()
            await dropoff_input.fill(endereco_cliente)
            await page.wait_for_timeout(1500)

            primeira_sugestao_dest = page.locator(
                '[data-testid="location-suggestion"], '
                'li[role="option"], '
                'div[class*="suggestion"]'
            ).first
            await primeira_sugestao_dest.click()
            await page.wait_for_timeout(2000)

            # ── Aguarda o mapa e as opções de entrega ──
            await page.wait_for_selector(
                '[data-testid="fare-estimate"], '
                'div[class*="fare"], '
                'div[class*="price"]',
                timeout=15_000,
            )
            await page.wait_for_timeout(2000)

            # ── Raspa o valor de "Envios Moto" ──
            opcoes = await page.locator(
                '[data-testid="vehicle-option"], '
                'div[class*="vehicle-option"], '
                'div[class*="product-option"]'
            ).all()

            valor_moto = None
            for opcao in opcoes:
                texto = await opcao.inner_text()
                if 'moto' in texto.lower() or 'envios moto' in texto.lower():
                    # Extrai valor numérico (ex: "R$ 12,50" -> 12.50)
                    match = re.search(r'R\$\s*([\d.,]+)', texto)
                    if match:
                        valor_str = match.group(1).replace('.', '').replace(',', '.')
                        valor_moto = float(valor_str)
                        break

            await browser.close()

            if valor_moto is not None:
                logger.info('Frete cotado: R$ %.2f', valor_moto)
                return {
                    'sucesso': True,
                    'valor_frete': valor_moto,
                    'mensagem': f'Frete Uber Moto: R$ {valor_moto:.2f}',
                }

            # Fallback: tenta raspar qualquer valor na página
            page_text = await page.locator('body').inner_text()
            match = re.search(r'R\$\s*([\d.,]+)', page_text)
            if match:
                valor_str = match.group(1).replace('.', '').replace(',', '.')
                valor_fallback = float(valor_str)
                logger.warning('Fallback frete: R$ %.2f', valor_fallback)
                return {
                    'sucesso': True,
                    'valor_frete': valor_fallback,
                    'mensagem': f'Frete estimado: R$ {valor_fallback:.2f}',
                }

            return {
                'sucesso': False,
                'valor_frete': None,
                'mensagem': 'Não foi possível encontrar o valor do frete na página.',
            }

    except PlaywrightTimeout:
        logger.error('Timeout no scraping do Uber')
        return {
            'sucesso': False,
            'valor_frete': None,
            'mensagem': 'Timeout ao acessar a página do Uber. Tente novamente.',
        }
    except Exception:
        logger.exception('Erro inesperado no scraping do Uber')
        return {
            'sucesso': False,
            'valor_frete': None,
            'mensagem': 'Erro ao cotar frete. Um atendente irá ajudá-lo.',
        }

```

## Arquivo: `core/urls.py`

```python
from django.urls import path
from . import views

app_name = 'core'

urlpatterns = [
    path('evolution/', views.evolution_webhook, name='evolution_webhook'),
]

```

## Arquivo: `core/views.py`

```python
"""Webhook da Evolution API e roteamento da máquina de estados.

View assíncrona que recebe os webhooks POST da Evolution API,
identifica a sessão do cliente e roteia para o handler correto
com base no estado atual da sessão.
"""

import asyncio
import json
import logging

from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import SessaoChat
from .llm_service import (
    processar_mensagem, LLMResult, MENU_TABACARIA, CATALOGO,
    gerar_secoes_lista, gerar_secoes_variantes,
    buscar_item, item_tem_variantes,
)
from .notificacoes import (
    enviar_whatsapp, notificar_teams,
    enviar_lista_whatsapp, enviar_botoes_whatsapp, enviar_imagem_whatsapp,
)
from .scraper import cotar_frete_uber

logger = logging.getLogger(__name__)


def _extrair_dados_webhook(body: dict) -> tuple[str, str, str] | None:
    """Extrai telefone, texto e tipo de interação do payload da Evolution API.

    Returns:
        Tupla (telefone, texto, tipo_interacao) ou None se não for
        mensagem processável. tipo_interacao pode ser 'texto', 'lista'
        ou 'botao'.
    """
    try:
        # Evolution API v2 payload structure
        data = body.get('data', body)
        key = data.get('key', {})
        telefone_raw = key.get('remoteJid', '')
        # Remove @s.whatsapp.net
        telefone = telefone_raw.split('@')[0] if '@' in telefone_raw else telefone_raw

        # Ignora mensagens enviadas pelo próprio bot
        if key.get('fromMe', False):
            return None

        message = data.get('message', {})

        # ── Verifica respostas interativas primeiro ──
        # Resposta de lista (seleção de item)
        list_reply = (
            message.get('listResponseMessage', {})
            .get('singleSelectReply', {})
            .get('selectedRowId')
        )
        if list_reply and telefone:
            return telefone, list_reply, 'lista'

        # Resposta de botão
        button_reply = message.get('buttonsResponseMessage', {}).get(
            'selectedButtonId',
        )
        if not button_reply:
            button_reply = message.get('templateButtonReplyMessage', {}).get(
                'selectedId',
            )
        if button_reply and telefone:
            return telefone, button_reply, 'botao'

        # ── Mensagem de texto convencional ──
        texto = (
            message.get('conversation')
            or message.get('extendedTextMessage', {}).get('text')
            or ''
        )

        if not telefone or not texto.strip():
            return None

        return telefone, texto.strip(), 'texto'
    except Exception:
        logger.exception('Erro ao extrair dados do webhook')
        return None


async def _obter_ou_criar_sessao(telefone: str) -> SessaoChat:
    """Obtém ou cria uma sessão de chat para o telefone."""
    sessao, _ = await SessaoChat.objects.aget_or_create(
        telefone=telefone,
        defaults={
            'estado_atual': SessaoChat.Estado.ATENDIMENTO_LLM,
            'carrinho_temporario': {},
            'historico_mensagens': [],
        },
    )
    return sessao


# ── Handlers de interação interativa ──────────────────────────────


async def _handler_selecao_lista(sessao: SessaoChat, item_id: str) -> None:
    """Processa seleção de item na lista interativa do cardápio."""
    item = buscar_item(item_id)
    if not item:
        await enviar_whatsapp(sessao.telefone, '😕 Item não encontrado. Tente novamente!')
        return

    # Se o item possui variantes, envia lista de variantes
    if item_tem_variantes(item_id):
        secoes = gerar_secoes_variantes(item_id)
        # Envia imagem do produto se disponível
        if item.get('imagem_url'):
            await enviar_imagem_whatsapp(
                sessao.telefone,
                item['imagem_url'],
                f"*{item['nome']}* — R$ {item['preco']:.2f}",
            )
        await enviar_lista_whatsapp(
            sessao.telefone,
            titulo=item['nome'],
            descricao='Escolha o sabor/cor desejado:',
            botao_texto='🎨 Escolher',
            secoes=secoes,
            rodape=f"R$ {item['preco']:.2f}",
        )
        # Salva estado de seleção pendente no carrinho_temporario
        sessao.carrinho_temporario['_selecao_pendente'] = item_id
        await sessao.asave(update_fields=['carrinho_temporario'])
    else:
        # Sem variantes — envia imagem e adiciona à conversa
        if item.get('imagem_url'):
            await enviar_imagem_whatsapp(
                sessao.telefone,
                item['imagem_url'],
                f"*{item['nome']}* — R$ {item['preco']:.2f}",
            )
        # Alimenta o LLM como se o usuário tivesse digitado
        msg = f"Quero adicionar {item['nome']} ao pedido"
        resultado, novo_historico = await processar_mensagem(
            historico=sessao.historico_mensagens,
            mensagem_usuario=msg,
        )
        sessao.historico_mensagens = novo_historico
        sessao.ultima_interacao = timezone.now()
        await sessao.asave(update_fields=['historico_mensagens', 'ultima_interacao'])
        if resultado.tipo == 'texto':
            await enviar_whatsapp(sessao.telefone, resultado.conteudo)


async def _handler_selecao_variante(sessao: SessaoChat, variante_id: str) -> None:
    """Processa seleção de variante (sabor/cor) de um item."""
    item_id = sessao.carrinho_temporario.get('_selecao_pendente', '')
    item = buscar_item(item_id)
    if not item:
        await enviar_whatsapp(sessao.telefone, '😕 Erro ao processar. Tente novamente!')
        return

    # Identifica o nome da variante selecionada
    variante_nome = ''
    for v in item.get('variantes', []):
        if v['id'] == variante_id:
            variante_nome = v['nome']
            if v.get('imagem_url'):
                await enviar_imagem_whatsapp(
                    sessao.telefone,
                    v['imagem_url'],
                    f"*{item['nome']}* — {variante_nome}",
                )
            break

    # Limpa seleção pendente
    sessao.carrinho_temporario.pop('_selecao_pendente', None)
    await sessao.asave(update_fields=['carrinho_temporario'])

    # Alimenta o LLM
    msg = f"Quero adicionar {item['nome']} sabor/cor {variante_nome} ao pedido"
    resultado, novo_historico = await processar_mensagem(
        historico=sessao.historico_mensagens,
        mensagem_usuario=msg,
    )
    sessao.historico_mensagens = novo_historico
    sessao.ultima_interacao = timezone.now()
    await sessao.asave(update_fields=['historico_mensagens', 'ultima_interacao'])
    if resultado.tipo == 'texto':
        await enviar_whatsapp(sessao.telefone, resultado.conteudo)


async def _handler_botao(sessao: SessaoChat, botao_id: str) -> None:
    """Processa clique em botão interativo."""
    if botao_id == 'ver_cardapio':
        secoes = gerar_secoes_lista()
        await enviar_lista_whatsapp(
            sessao.telefone,
            titulo='📋 Cardápio Robopod',
            descricao='Selecione um produto para ver detalhes:',
            botao_texto='📋 Ver Cardápio',
            secoes=secoes,
            rodape='Delivery Noturno 🌙',
        )
    elif botao_id == 'falar_humano':
        sessao.estado_atual = SessaoChat.Estado.ATENDIMENTO_HUMANO
        await sessao.asave(update_fields=['estado_atual'])
        await enviar_whatsapp(
            sessao.telefone,
            '🔄 Transferindo para um atendente humano... Aguarde um momento, por favor!',
        )
        await notificar_teams(
            f'🚨 *Transferência para humano*\n'
            f'📱 Cliente: {sessao.telefone}\n'
            f'📝 Motivo: Cliente solicitou via botão\n'
            f'🕐 {timezone.now().strftime("%d/%m/%Y %H:%M")}',
        )
    elif botao_id == 'confirmar_pedido':
        # Simula texto de confirmação para o LLM
        await _handler_atendimento_llm(sessao, 'Sim, confirmo o pedido')
    elif botao_id == 'alterar_pedido':
        await enviar_whatsapp(sessao.telefone, '✏️ Ok! Me diga o que deseja alterar no pedido.')
    elif botao_id == 'cancelar_pedido':
        sessao.resetar_sessao()
        await sessao.asave()
        await enviar_whatsapp(sessao.telefone, '❌ Pedido cancelado. Se precisar de algo, é só chamar! 😊')
    else:
        # Botão desconhecido — trata como texto
        await _handler_atendimento_llm(sessao, botao_id)


# ── Handlers de estado ────────────────────────────────────────────


async def _handler_atendimento_llm(
    sessao: SessaoChat, texto: str,
) -> None:
    """Processa mensagem no estado de atendimento via LLM."""
    # Primeira mensagem da sessão — envia boas-vindas com botões
    if not sessao.historico_mensagens:
        await enviar_whatsapp(
            sessao.telefone,
            f'Olá! 👋 Bem-vindo à *Robopod* — Delivery Noturno de Tabacaria!\n\n'
            f'Como posso te ajudar?',
        )
        await enviar_botoes_whatsapp(
            sessao.telefone,
            titulo='O que deseja fazer?',
            descricao='Escolha uma opção abaixo:',
            botoes=[
                {'type': 'reply', 'displayText': '📋 Ver Cardápio', 'id': 'ver_cardapio'},
                {'type': 'reply', 'displayText': '👤 Falar com Atendente', 'id': 'falar_humano'},
            ],
        )
        sessao.historico_mensagens = [
            {'role': 'user', 'content': texto},
            {'role': 'assistant', 'content': 'Olá! Bem-vindo à Robopod! Como posso te ajudar?'},
        ]
        sessao.ultima_interacao = timezone.now()
        await sessao.asave(update_fields=['historico_mensagens', 'ultima_interacao'])
        return

    resultado, novo_historico = await processar_mensagem(
        historico=sessao.historico_mensagens,
        mensagem_usuario=texto,
    )

    sessao.historico_mensagens = novo_historico
    sessao.ultima_interacao = timezone.now()

    if resultado.tipo == 'texto':
        await enviar_whatsapp(sessao.telefone, resultado.conteudo)
        await sessao.asave(
            update_fields=['historico_mensagens', 'ultima_interacao'],
        )

    elif resultado.tipo == 'fechar_pedido':
        sessao.carrinho_temporario = resultado.dados
        sessao.estado_atual = SessaoChat.Estado.AGUARDANDO_ENDERECO
        await sessao.asave(update_fields=[
            'historico_mensagens', 'ultima_interacao',
            'carrinho_temporario', 'estado_atual',
        ])

        # Monta resumo do carrinho
        itens = resultado.dados.get('itens', [])
        total = sum(i['preco_unitario'] * i['quantidade'] for i in itens)
        resumo_linhas = [f"  • {i['quantidade']}x {i['nome']} — R$ {i['preco_unitario'] * i['quantidade']:.2f}" for i in itens]
        resumo = '\n'.join(resumo_linhas)

        await enviar_whatsapp(
            sessao.telefone,
            f"✅ Pedido confirmado!\n\n"
            f"{resumo}\n\n"
            f"💰 Subtotal: R$ {total:.2f}\n\n"
            f"📍 Agora preciso do seu endereço completo para calcular o frete.\n"
            f"(Rua, número, bairro, cidade)",
        )

        # Envia botões de confirmação do pedido
        await enviar_botoes_whatsapp(
            sessao.telefone,
            titulo='Confirmar pedido?',
            descricao=f'Subtotal: R$ {total:.2f}',
            botoes=[
                {'type': 'reply', 'displayText': '✅ Confirmar', 'id': 'confirmar_pedido_final'},
                {'type': 'reply', 'displayText': '✏️ Alterar', 'id': 'alterar_pedido'},
                {'type': 'reply', 'displayText': '❌ Cancelar', 'id': 'cancelar_pedido'},
            ],
        )

    elif resultado.tipo == 'solicitar_humano':
        sessao.estado_atual = SessaoChat.Estado.ATENDIMENTO_HUMANO
        await sessao.asave(update_fields=[
            'historico_mensagens', 'ultima_interacao', 'estado_atual',
        ])
        await enviar_whatsapp(
            sessao.telefone,
            '🔄 Transferindo para um atendente humano... '
            'Aguarde um momento, por favor!',
        )
        motivo = resultado.dados.get('motivo', 'Não especificado')
        await notificar_teams(
            f'🚨 *Transferência para humano*\n'
            f'📱 Cliente: {sessao.telefone}\n'
            f'📝 Motivo: {motivo}\n'
            f'🕐 {timezone.now().strftime("%d/%m/%Y %H:%M")}',
        )


async def _handler_aguardando_endereco(
    sessao: SessaoChat, texto: str,
) -> None:
    """Recebe endereço do cliente e dispara cotação de frete em background."""
    sessao.estado_atual = SessaoChat.Estado.CALCULANDO_FRETE
    sessao.ultima_interacao = timezone.now()
    await sessao.asave(update_fields=['estado_atual', 'ultima_interacao'])

    await enviar_whatsapp(
        sessao.telefone,
        '🔍 Calculando o frete para o seu endereço... Aguarde um momento!',
    )

    # Dispara scraping em background para não bloquear o webhook
    asyncio.create_task(
        _task_calcular_frete(sessao.telefone, texto),
        name=f'frete_{sessao.telefone}',
    )


async def _task_calcular_frete(telefone: str, endereco: str) -> None:
    """Task de background: cota frete, soma totais e envia PIX."""
    try:
        # Busca sessão atualizada
        sessao = await SessaoChat.objects.aget(telefone=telefone)

        # Cota frete via Uber
        resultado_frete = await cotar_frete_uber(endereco)

        if not resultado_frete['sucesso']:
            # Frete falhou — escala para humano
            sessao.estado_atual = SessaoChat.Estado.ATENDIMENTO_HUMANO
            await sessao.asave(update_fields=['estado_atual'])
            await enviar_whatsapp(
                telefone,
                '😕 Não consegui calcular o frete automaticamente. '
                'Um atendente irá ajudá-lo em instantes!',
            )
            await notificar_teams(
                f'⚠️ *Falha na cotação de frete*\n'
                f'📱 Cliente: {telefone}\n'
                f'📍 Endereço: {endereco}\n'
                f'❌ Erro: {resultado_frete["mensagem"]}',
            )
            return

        # Calcula total
        valor_frete = resultado_frete['valor_frete']
        itens = sessao.carrinho_temporario.get('itens', [])
        subtotal = sum(i['preco_unitario'] * i['quantidade'] for i in itens)
        total = subtotal + valor_frete

        # Atualiza sessão
        sessao.estado_atual = SessaoChat.Estado.AGUARDANDO_COMPROVANTE
        sessao.carrinho_temporario['frete'] = valor_frete
        sessao.carrinho_temporario['total'] = total
        sessao.carrinho_temporario['endereco'] = endereco
        sessao.ultima_interacao = timezone.now()
        await sessao.asave(update_fields=[
            'estado_atual', 'carrinho_temporario', 'ultima_interacao',
        ])

        # Monta mensagem de cobrança
        resumo_linhas = [
            f"  • {i['quantidade']}x {i['nome']} — "
            f"R$ {i['preco_unitario'] * i['quantidade']:.2f}"
            for i in itens
        ]
        resumo = '\n'.join(resumo_linhas)

        mensagem_cobranca = (
            f"🧾 *Resumo do Pedido*\n\n"
            f"{resumo}\n\n"
            f"🚚 Frete ({resultado_frete['mensagem']}): R$ {valor_frete:.2f}\n"
            f"💰 *TOTAL: R$ {total:.2f}*\n\n"
            f"────────────────────\n"
            f"💳 *Pagamento via PIX*\n\n"
            f"Chave PIX: `{settings.CHAVE_PIX}`\n\n"
            f"Após o pagamento, envie o comprovante aqui neste chat. ✅"
        )

        await enviar_whatsapp(telefone, mensagem_cobranca)

        # Notifica equipe no Teams
        await notificar_teams(
            f'🛒 *Novo pedido aguardando pagamento*\n'
            f'📱 Cliente: {telefone}\n'
            f'📍 Endereço: {endereco}\n'
            f'💰 Total: R$ {total:.2f}\n'
            f'🚚 Frete: R$ {valor_frete:.2f}',
        )

    except Exception:
        logger.exception('Erro na task de cálculo de frete para %s', telefone)
        try:
            await enviar_whatsapp(
                telefone,
                '😕 Ocorreu um erro. Um atendente irá ajudá-lo em breve!',
            )
        except Exception:
            logger.exception('Falha ao enviar mensagem de erro')


async def _handler_aguardando_comprovante(
    sessao: SessaoChat, texto: str,
) -> None:
    """Lembra o cliente de enviar o comprovante."""
    await enviar_whatsapp(
        sessao.telefone,
        '⏳ Ainda estamos aguardando o comprovante de pagamento PIX.\n'
        f'Chave PIX: `{settings.CHAVE_PIX}`\n\n'
        'Envie a foto ou PDF do comprovante aqui. '
        'Se precisar de ajuda, digite "atendente".',
    )


async def _handler_atendimento_humano(
    sessao: SessaoChat, texto: str,
) -> None:
    """Notifica que a conversa está com um humano."""
    await notificar_teams(
        f'💬 *Mensagem do cliente (atendimento humano)*\n'
        f'📱 {sessao.telefone}: {texto}',
    )


# ── Mapa de handlers por estado ──
_HANDLERS = {
    SessaoChat.Estado.ATENDIMENTO_LLM: _handler_atendimento_llm,
    SessaoChat.Estado.AGUARDANDO_ENDERECO: _handler_aguardando_endereco,
    SessaoChat.Estado.CALCULANDO_FRETE: lambda s, t: enviar_whatsapp(
        s.telefone, '⏳ Ainda estou calculando o frete... Aguarde!',
    ),
    SessaoChat.Estado.AGUARDANDO_COMPROVANTE: _handler_aguardando_comprovante,
    SessaoChat.Estado.ATENDIMENTO_HUMANO: _handler_atendimento_humano,
}


@csrf_exempt
@require_POST
async def evolution_webhook(request) -> JsonResponse:
    """Endpoint principal do webhook da Evolution API.

    Recebe POST com o payload da mensagem, identifica o estado
    da sessão e roteia para o handler apropriado.
    """
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'JSON inválido'}, status=400)

    # Filtra apenas eventos de mensagem recebida
    evento = body.get('event', '')
    if evento not in ('messages.upsert', 'MESSAGES_UPSERT', ''):
        return JsonResponse({'status': 'evento ignorado'})

    dados = _extrair_dados_webhook(body)
    if dados is None:
        return JsonResponse({'status': 'mensagem ignorada'})

    telefone, texto, tipo_interacao = dados
    logger.info('Mensagem recebida de %s [%s]: %s', telefone, tipo_interacao, texto[:100])

    # Obtém ou cria sessão
    sessao = await _obter_ou_criar_sessao(telefone)

    # ── Respostas interativas (apenas no estado de atendimento LLM) ──
    if tipo_interacao == 'lista' and sessao.estado_atual == SessaoChat.Estado.ATENDIMENTO_LLM:
        # Verifica se há seleção de variante pendente
        if sessao.carrinho_temporario.get('_selecao_pendente'):
            await _handler_selecao_variante(sessao, texto)
        else:
            await _handler_selecao_lista(sessao, texto)
        return JsonResponse({'status': 'ok'})

    if tipo_interacao == 'botao' and sessao.estado_atual == SessaoChat.Estado.ATENDIMENTO_LLM:
        await _handler_botao(sessao, texto)
        return JsonResponse({'status': 'ok'})

    # ── Tratamento de texto normal via máquina de estados ──
    handler = _HANDLERS.get(sessao.estado_atual)
    if handler:
        # Para o estado calculando_frete, não precisa await do lambda
        if sessao.estado_atual == SessaoChat.Estado.CALCULANDO_FRETE:
            await handler(sessao, texto)
        else:
            await handler(sessao, texto)
    else:
        logger.warning('Estado desconhecido: %s', sessao.estado_atual)

    # Retorna 200 imediato (obrigatório para webhooks)
    return JsonResponse({'status': 'ok'})

```

## Arquivo: `core/__init__.py`

```python

```

## Arquivo: `robopod/asgi.py`

```python
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'robopod.settings')
django.setup()

from django.core.asgi import get_asgi_application

application = get_asgi_application()

```

## Arquivo: `robopod/settings.py`

```python
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', 'insecure-dev-key-change-me')
DEBUG = os.getenv('DEBUG', 'True').lower() in ('true', '1', 'yes')
ALLOWED_HOSTS = os.getenv('ALLOWED_HOSTS', '*').split(',')

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'core',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'robopod.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

ASGI_APPLICATION = 'robopod.asgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.getenv('POSTGRES_DB', 'robopod'),
        'USER': os.getenv('POSTGRES_USER', 'robopod'),
        'PASSWORD': os.getenv('POSTGRES_PASSWORD', 'robopod_secret'),
        'HOST': os.getenv('POSTGRES_HOST', 'db'),
        'PORT': os.getenv('POSTGRES_PORT', '5432'),
    }
}

LANGUAGE_CODE = 'pt-br'
TIME_ZONE = 'America/Sao_Paulo'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ── Chaves de API ──
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')
OPENAI_MODEL = os.getenv('OPENAI_MODEL', 'gpt-4o-mini')

# ── Evolution API ──
EVOLUTION_API_URL = os.getenv('EVOLUTION_API_URL', 'http://localhost:8080')
EVOLUTION_API_KEY = os.getenv('EVOLUTION_API_KEY', '')
EVOLUTION_INSTANCE = os.getenv('EVOLUTION_INSTANCE', 'robopod')

# ── Uber Scraping ──
ENDERECO_LOJA = os.getenv('ENDERECO_LOJA', 'Rua Exemplo, 123 - Centro, São Paulo - SP')

# ── Teams Webhook ──
TEAMS_WEBHOOK_URL = os.getenv('TEAMS_WEBHOOK_URL', '')

# ── PIX ──
CHAVE_PIX = os.getenv('CHAVE_PIX', '')

```

## Arquivo: `robopod/urls.py`

```python
from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    path('webhook/', include('core.urls')),
]

```

## Arquivo: `robopod/__init__.py`

```python

```

