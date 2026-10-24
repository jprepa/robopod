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
7. [Segurança e dados dos clientes](#7-segurança-e-dados-dos-clientes)
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
│  Frete     Aviso no            │
│  (moto)    grupo WhatsApp      │
│    │                           │
│    ▼                           │
│  Cobrança PIX                  │
│  via WhatsApp                  │
└────────────────────────────────┘
```

**Fluxo Híbrido:**
- **Fase 1 (LLM):** A IA atua como garçom digital com guardrails rígidos.
  Ela interpreta o que o cliente escreveu ("o de melancia", "cereja", erro de digitação)
  e escolhe modelo + sabor do cardápio; o código confere a escolha (`conferir_itens`)
  antes de enviar o resumo "Confere seu pedido" (`confirmar_itens`) e de novo no
  `fechar_pedido`. Sabor ou modelo que não existe nunca é confirmado: o cliente
  recebe os sabores disponíveis daquele modelo para escolher.
- **Fase 2 (Determinística):** Django assume para cotação de frete e cobrança.
- **Escape (Humano):** Botão de pânico transfere para atendente.

---

## 2. Pré-requisitos

| Ferramenta      | Versão Mínima | Instalação                          |
|-----------------|---------------|-------------------------------------|
| WSL2            | Ubuntu 22.04+ | `wsl --install`                     |
| Docker Desktop  | 4.0+          | https://docker.com/desktop          |
| Docker Compose  | v2+           | Incluso no Docker Desktop           |
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

### 3.3.1. Vários números (lojas)

A operação pode ter vários números de WhatsApp, cada um uma instância da Evolution, todos
ligados ao mesmo painel, cardápio, estoque, equipe e configurações. Cadastre em
**Configurações → Lojas** (só administradores): o painel cria a instância na Evolution e
mostra o QR Code para conectar o número.

- O webhook é **global** (`WEBHOOK_GLOBAL_*` no `docker-compose.yml`): toda mensagem chega
  com o nome da instância, e o app descobre por qual loja ela entrou. Instância não
  cadastrada ou loja desativada: a mensagem é ignorada.
- O cliente é um só (identificado pelo número dele). `Cliente.loja_origem` guarda o número
  que ele chamou na primeira vez e `Cliente.loja_atual` o da última mensagem: o bot e os
  atendentes respondem por ele (`painel/lojas.py`).
- Cada pedido e cada mensagem guardam a loja. Com mais de uma loja ativa, Conversas e
  Pedidos ganham o filtro por loja e a etiqueta com o nome dela, e os avisos ao grupo
  dizem por qual número o cliente chamou.
- Os avisos ao grupo e a lista de grupos usam a loja **principal**, que precisa
  participar do grupo.
- A mesma mensagem entregue duas vezes pela Evolution é tratada uma vez só (id da
  mensagem guardado por 10 minutos).

### 3.4. Grupo de avisos no WhatsApp

O bot avisa a equipe num grupo do WhatsApp: cliente pediu atendente, pedido novo,
comprovante recebido e falha no frete.

1. Crie um grupo no WhatsApp com a equipe e **adicione o número da loja** (o do bot).
2. No painel, abra **Configurações**, escolha o grupo na lista e clique em **Salvar**.
3. Clique em **Enviar mensagem de teste** para confirmar.
4. Opcional: preencha `PAINEL_URL` no `.env` para os avisos virem com link da conversa.

Mensagens enviadas dentro de grupos são ignoradas pelo bot, então a equipe pode
conversar no grupo à vontade.

### 3.4.1. Cardápio em PDF

Em **Configurações › Cardápio em PDF**, envie o PDF do cardápio (até 30 MB). Ele vai
para todo cliente novo junto das boas-vindas, antes da lista de modelos e preços. O
arquivo fica em `media/cardapio/` na pasta do projeto (fora do git) e pode ser trocado
ou removido ali mesmo.

Quem fica mais de 4 horas sem falar com o bot recebe as boas-vindas de novo na
próxima mensagem.

### 3.4.2. Aba Início e expediente

A aba **Início** (primeira do menu, abre depois do login) junta o que precisa de
atenção agora: clientes esperando atendente, comprovantes para conferir, pedidos pagos
esperando sair, pedidos sem pagamento há mais de 30 min, itens esgotados, o movimento
do dia por hora e os pedidos em andamento. Atualiza sozinha a cada 20 segundos.

O botão **Encerrar atividades** pausa o bot: quem mandar mensagem recebe só a
*mensagem de loja fechada* com o horário de atendimento (editáveis ali mesmo, com
prévia). Cada cliente recebe esse aviso no máximo uma vez a cada 3 horas. Pedidos já em
andamento (endereço, frete, comprovante) e conversas com atendente seguem normalmente.
O selo **Loja aberta / Loja fechada** abaixo do nome no menu mostra o estado em todas
as telas. Ao **Abrir a loja**, a Início lista quem chamou enquanto estava fechada e
ainda não teve resposta, para a equipe chamar de volta.

O botão **Pausar bot** deixa o bot em silêncio total: ninguém recebe resposta
automática, nem a mensagem de loja fechada. As mensagens continuam chegando em
**Conversas** para a equipe responder pelo painel (o selo do menu fica **Bot pausado**).
Só o comprovante de pagamento segue sendo registrado, sem resposta ao cliente.

### 3.4.3. Áudios dos clientes

As notas de voz são transcritas pela OpenAI e seguem como se o cliente tivesse
escrito; no painel aparecem como "🎤 Áudio: …". O modelo é `OPENAI_TRANSCRICAO_MODEL`
(padrão `gpt-4o-mini-transcribe`; `whisper-1` também funciona). O áudio é baixado da
Evolution pelo id da mensagem, então não precisa ligar o `webhook_base64`. Se a
transcrição falhar, o bot pede para o cliente escrever.

### 3.4.4. Fotos dos clientes

No atendimento, uma foto (ex.: print de um pod) é descrita pelo `OPENAI_MODEL`
(precisa aceitar imagem, como o `gpt-4o-mini`) usando os nomes do cardápio, e a
descrição segue para a IA como se o cliente tivesse escrito, junto com a legenda.
Se não der para ler a foto, ou se for um documento, o bot pede para o cliente
escrever. Na etapa de pagamento, foto/documento continua valendo como comprovante.

### 3.5. Chave PIX

```env
CHAVE_PIX=seu-email@pix.com
# ou CPF, CNPJ, telefone, chave aleatória
```

---

## 4. Variáveis de Ambiente

| Variável              | Obrigatória | Descrição                                      |
|-----------------------|:-----------:|-------------------------------------------------|
| `DJANGO_SECRET_KEY`   | Sim         | Chave secreta do Django (sem ela o app não sobe com `DEBUG=False`) |
| `DEBUG`               | Não         | `True` só no PC de desenvolvimento (padrão: `False`) |
| `ALLOWED_HOSTS`       | Não         | Endereços aceitos (padrão: `localhost,127.0.0.1,app`; acrescente o do Tailscale) |
| `PAINEL_HTTPS`        | Não         | `true` quando o painel abre por https: cookies seguros e HSTS |
| `WEBHOOK_TOKEN`       | Sim         | Token secreto que a Evolution manda na URL do webhook (`?token=`) |
| `OPENAI_API_KEY`      | Sim         | Chave da API da OpenAI                          |
| `OPENAI_MODEL`        | Não         | Modelo a usar (padrão: `gpt-4o-mini`)           |
| `OPENAI_TRANSCRICAO_MODEL` | Não    | Transcrição de áudios (padrão: `gpt-4o-mini-transcribe`) |
| `EVOLUTION_API_URL`   | Sim         | URL base da Evolution API                       |
| `EVOLUTION_API_KEY`   | Sim         | Chave de autenticação da Evolution API          |
| `EVOLUTION_INSTANCE`  | Sim         | Instância do primeiro número (vira a Loja 1 na migração; as demais são cadastradas no painel) |
| `EVOLUTION_VERSION`   | Não         | Versão fixa da imagem da Evolution (padrão: `latest`) |
| `ENDERECO_LOJA`       | Sim         | Endereço completo da loja (origem do frete)     |
| `FRETE_MODO`          | Não         | `estimativa` (padrão) ou `uber_scraper`         |
| `GOOGLE_MAPS_API_KEY` | Não         | Chave c/ Routes API; vazio = OpenStreetMap      |
| `FRETE_TAXA_BASE`     | Não         | Taxa fixa do frete em R$ (padrão: `6.00`)       |
| `FRETE_VALOR_KM`      | Não         | R$ por km de rota (padrão: `1.50`)              |
| `FRETE_VALOR_MINIMO`  | Não         | Frete mínimo em R$ (padrão: `9.00`)             |
| `FRETE_MARGEM_PERCENTUAL` | Não     | % somado ao frete (padrão: `0`)                 |
| `FRETE_DISTANCIA_MAXIMA_KM` | Não   | Acima disso vai p/ atendente (padrão: `20`)     |
| `FRETE_CIDADE_PADRAO` | Não       | Cidade anexada se o cliente não informar (padrão: fim do `ENDERECO_LOJA`) |
| `LOJA_COORDENADAS`    | Não         | `lat,lng` fixos da loja (recomendado)           |
| `WHATSAPP_MENU_INTERATIVO` | Não    | `true` = cardápio em lista interativa           |
| `CARDAPIO_IMAGEM_URL` | Não         | Imagem enviada antes do cardápio                |
| `PAINEL_URL`          | Não         | Endereço público do painel (link nos avisos)    |
| `CHAVE_PIX`           | Sim         | Chave PIX para cobrança                         |
| `CSRF_TRUSTED_ORIGINS` | Não        | URL(s) do painel com https, separadas por vírgula |
| `PAINEL_VIRADA_DIA_HORA` | Não      | Hora em que vira o dia nos relatórios (padrão: `6`) |
| `POSTGRES_DB`         | Não         | Nome do banco (padrão: `robopod`)               |
| `POSTGRES_USER`       | Não         | Usuário do banco (padrão: `robopod`)            |
| `POSTGRES_PASSWORD`   | Sim         | Senha do banco (o docker-compose não sobe sem ela) |
| `LOGIN_TENTATIVAS_MAXIMAS` | Não    | Senhas erradas seguidas até bloquear o login (padrão: `5`) |
| `LOGIN_BLOQUEIO_MINUTOS` | Não      | Duração do bloqueio do login (padrão: `15`)     |
| `RETENCAO_ENDERECO_DIAS` | Não      | Dias até apagar o endereço do pedido entregue/cancelado (padrão: `3`) |
| `RETENCAO_MENSAGENS_DIAS` | Não     | Dias até apagar o histórico das conversas (padrão: `3`) |

---

## 5. Subindo o Projeto

### 5.1. Com Docker Compose (recomendado)

```bash
# Produção (VPS): build e sobe tudo
docker compose up --build -d

# Desenvolvimento (PC, com DEBUG=True no .env): código recarregado a cada
# alteração e banco em localhost:5432
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build

# Verifica logs
docker compose logs -f app

# Cria superusuário para o admin
docker compose exec app python manage.py createsuperuser
```

O app fica em `http://localhost:8000` e o admin do Django em `http://localhost:8000/admin/`,
**só na própria máquina** (as portas escutam em `127.0.0.1`). Na VPS a equipe acessa pelo
Tailscale (veja a seção 7 e o `GUIA_DEPLOY_VPS.md`).

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

## 7. Segurança e dados dos clientes

**Nada fica aberto para a internet.** A Evolution conversa com o WhatsApp saindo do
servidor, e o webhook chega ao Django pela rede interna do Docker (`http://app:8000`).
Por isso nenhuma porta precisa ficar aberta:

| Serviço | Onde escuta | Quem acessa |
|---|---|---|
| Painel e admin (Django) | `127.0.0.1:8000` | A equipe, pelo Tailscale (`tailscale serve`) |
| Manager da Evolution (QR Code) | `127.0.0.1:8083` | Só o administrador, pelo Tailscale |
| PostgreSQL | sem porta | Só os containers (`docker compose exec db psql ...`) |

**Proteções no app**

- `DEBUG` desligado por padrão e o app não sobe sem `DJANGO_SECRET_KEY` de verdade.
- Webhook só aceita quem manda o `WEBHOOK_TOKEN` (na URL `?token=` ou no cabeçalho
  `X-Webhook-Token`); sem o token responde `403`.
- Proteção CSRF em todos os formulários do painel.
- Login bloqueado por `LOGIN_BLOQUEIO_MINUTOS` depois de `LOGIN_TENTATIVAS_MAXIMAS` senhas
  erradas seguidas (por usuário e por IP). Reiniciar o app zera os bloqueios.
- Login expira em 12 h. Com `PAINEL_HTTPS=true`, cookies só trafegam por https.
- A imagem Docker não leva `.env`, `media/` nem `.git` (`.dockerignore`).

**Dados dos clientes guardados por pouco tempo**

O serviço `limpeza` do docker-compose roda `python manage.py limpar_dados` uma vez por dia
(dá para rodar na mão: `docker compose exec app python manage.py limpar_dados --simular`).

| Dado | Quando sai |
|---|---|
| Comprovante PIX | Na hora em que o pagamento é confirmado ou o pedido é cancelado |
| Endereço do pedido | `RETENCAO_ENDERECO_DIAS` depois de entregue, cancelado ou abandonado sem pagamento; 30 dias para qualquer outro status esquecido |
| Mensagens das conversas (painel) | Mais antigas que `RETENCAO_MENSAGENS_DIAS` |
| Contexto da IA e endereço do carrinho | Sessões paradas há mais de `RETENCAO_MENSAGENS_DIAS` (o estado da conversa não muda) |
| Mensagens guardadas pela Evolution | Mais antigas que `RETENCAO_MENSAGENS_DIAS` (tabela `evolution."Message"`) |

Telefone e nome do cliente ficam: são o que identifica o cliente. O endereço não é mais
guardado na ficha do cliente, só no pedido.

> **Grupo de avisos:** os avisos ao grupo trazem o endereço do pedido. Ative as
> *mensagens temporárias* (24 horas) no grupo para essas cópias não ficarem nos celulares.

---

## 8. Evolution API — Configuração do Webhook

Na interface da Evolution API ou via cURL:

```bash
curl -X POST "${EVOLUTION_API_URL}/webhook/set/${EVOLUTION_INSTANCE}" \
  -H "Content-Type: application/json" \
  -H "apikey: ${EVOLUTION_API_KEY}" \
  -d '{
    "url": "http://app:8000/webhook/evolution/?token=SEU_WEBHOOK_TOKEN",
    "webhook_by_events": false,
    "webhook_base64": false,
    "events": [
      "MESSAGES_UPSERT"
    ]
  }'
```

`app` é o nome do serviço do Django no docker-compose: o webhook não sai da rede interna do
Docker. `SEU_WEBHOOK_TOKEN` é o valor do `WEBHOOK_TOKEN` do `.env`.

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
    ├── frete.py              # Cotação de frete (Lalamove / estimativa)
    └── notificacoes.py       # WhatsApp (Evolution) e avisos no grupo da equipe
│
└── painel/                   # Painel da loja (/painel/)
    ├── models.py             # Cliente, Categoria, Produto, Variante, Pedido, ItemPedido, Mensagem, RegistroAuditoria
    ├── auditoria.py          # Registro de quem fez o quê (aba Auditoria)
    ├── catalogo.py           # Cardápio do banco p/ o bot (cache + validação do carrinho)
    ├── servicos.py           # Clientes, histórico, pedidos e estoque
    ├── views.py / urls.py    # Telas do painel
    ├── catalogo_inicial.py   # Cardápio gravado na 1ª migração
    ├── management/commands/importar_cardapio.py  # Cardápio a partir do cardapio.txt
    └── templates/painel/     # HTML (Django + HTMX + Chart.js via CDN)
```

---

## 11. API de Referência

### Webhook Endpoint

```
POST /webhook/evolution/?token=SEU_WEBHOOK_TOKEN
Content-Type: application/json
```

Sem o token (ou com o token errado) responde `403`.

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

### Painel da loja

- URL: `http://localhost:8000/painel/` (a raiz `/` redireciona para ele)
- Login com o mesmo usuário do admin (`python manage.py createsuperuser`)
- Acesso por domínio/HTTPS: preencha `CSRF_TRUSTED_ORIGINS` no `.env`
  (ex.: `https://painel.seudominio.com.br`), senão o login dá erro 403.

| Seção | O que faz |
|-------|-----------|
| **Dashboard** | Faturamento de hoje e do período (7/30/90 dias) com variação, gráfico de faturamento por dia e mais vendidos (os alertas de conversas aguardando atendente e comprovantes a conferir ficam só na aba Início). Conta só pedidos pagos/em entrega/entregues. O "dia" vira às `PAINEL_VIRADA_DIA_HORA` (padrão 6h), então a venda da 01h entra na noite anterior. |
| **Conversas** | Caixa de entrada atualizada a cada 10 s, com filtros (aguardando atendente, não lidas, com pedido aberto) e busca por nome/número. Dentro da conversa, as mensagens novas aparecem sozinhas e dá para responder pelo painel (sai pelo WhatsApp da loja), usar respostas rápidas (botões ou `/comando`; veja abaixo), **assumir** a conversa (o bot para de responder) ou **devolver ao bot**. |
| **Clientes** | Cadastro por número, criado automaticamente na primeira mensagem (com o nome do perfil do WhatsApp). Mostra total gasto, nº de pedidos, último pedido e o que mais pede. Dá para editar nome/observações e **bloquear** o número (o bot passa a ignorá-lo). |
| **Pedidos** | Criados automaticamente quando o frete é cotado. O comprovante (imagem ou PDF) enviado pelo cliente fica salvo em `media/comprovantes/` e aparece direto na tela do pedido, ao lado do botão de status, sem precisar abrir o WhatsApp no celular (só para usuários logados). Ao confirmar o pagamento (status pago, saiu para entrega ou entregue) ou cancelar o pedido, o comprovante é **apagado automaticamente** do disco e do pedido. O endereço do pedido é apagado `RETENCAO_ENDERECO_DIAS` depois da entrega (veja *Segurança e dados dos clientes*). Status: aguardando pagamento → comprovante recebido → pago → saiu para entrega → entregue (ou cancelado). Cada mudança pode avisar o cliente no WhatsApp; cancelar devolve o estoque; entregue/cancelado devolve a conversa ao bot. |
| **Estoque** | Cardápio do bot: categorias, produtos, preços, sabores/opções e estoque, com ajuste rápido (+/−, preço, ocultar). Vale na hora para o bot. Estoque vazio = não controla; zerado ou oculto some do cardápio. O pedido reserva o estoque ao ser criado. |
| **Auditoria** | Só para superusuários. Registra quem fez cada ação, quando e de qual IP: logins (inclusive senha errada), respostas e assumir/devolver conversa, edição e bloqueio de cliente, mudança de status, venda manual, ajustes de estoque/preço, produtos, categorias e configurações. Mostra o antes → depois de cada campo e tem link para o item. Também registra o que o **Bot** faz sozinho (pedido criado, comprovante recebido). Cliques seguidos no +/− do estoque viram um registro só. Os registros não podem ser editados nem apagados (no admin ficam só para leitura). |

Para trocar o cardápio inteiro de uma vez (modelos, preços e sabores), edite
`cardapio.txt` e rode `python manage.py importar_cardapio` (`--simular` mostra antes o
que muda). Sabor com ❌ no começo da linha entra esgotado (estoque 0).

O bot agora usa o **preço do banco** (não o que a IA escreveu) e confere o estoque
ao fechar o pedido. Se faltar algo, avisa o cliente e pede para ajustar.

#### Respostas rápidas no chat

Na conversa, digite `/` na caixa de resposta para abrir o menu de comandos
(↑↓ escolhe, Enter/Tab insere, Esc fecha). O texto entra onde está o cursor e
pode ser editado antes de enviar.

- **Respostas rápidas** (`/oi`, `/pix`, `/obrigado`, `/rastreio`…): criadas em
  *Configurações → Respostas rápidas* ou direto no chat pelo botão **＋ Nova** (que
  aproveita o texto já digitado). Podem virar botão acima da caixa e aceitam as
  variáveis `{nome}` (primeiro nome), `{pix}`, `{endereco}`, `{pedido}` (#nº),
  `{total}` e `{horario}`.
- **Dados do cliente**: `/nome`, `/endereco` e `/pedido` (resumo do pedido em
  andamento com itens e total).
- **Modelos do cardápio**: `/` + parte do nome (`/elf45k`, `/v35`) insere o preço e
  os sabores em estoque, no mesmo formato que o bot usa.

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

1. Confira a URL cadastrada na Evolution API: `http://app:8000/webhook/evolution/?token=...`
   com o mesmo valor do `WEBHOOK_TOKEN` do `.env`
2. Verifique os logs do Django: `docker compose logs -f app`. `Webhook com token inválido
   recusado` = token diferente; `WEBHOOK_TOKEN não definido` = falta a variável no `.env`
3. Teste manualmente:

```bash
curl -X POST "http://localhost:8000/webhook/evolution/?token=SEU_WEBHOOK_TOKEN" \
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

### Frete não é cotado

- O `m.uber.com/go/delivery` **exige login**, por isso o scraper (`FRETE_MODO=uber_scraper`)
  não funciona sem sessão. O padrão é `FRETE_MODO=estimativa` (`core/frete.py`):
  `max(MINIMO, BASE + km × VALOR_KM) + MARGEM%`, arredondado para cima em R$ 0,50.
- Calibre `FRETE_TAXA_BASE` / `FRETE_VALOR_KM` comparando com cotações reais do Uber Flash.
- Sem `GOOGLE_MAPS_API_KEY` usa OpenStreetMap (grátis, pode não achar alguns endereços).
  Com a chave (Routes API ativada) a precisão melhora bastante.
- O endereço do cliente é localizado uma vez, priorizando a região da loja; se ele não
  citar a cidade, o bot tenta também com `FRETE_CIDADE_PADRAO` anexada. As mesmas
  coordenadas são usadas pela Lalamove e pela estimativa, e o resultado fica em cache.
  Chamadas aos mapas têm novas tentativas em timeout/429/5xx.
- O cliente vê "Localizado como: ..." no resumo para conferir o endereço entendido.
- Endereço não encontrado → o bot pede o endereço de novo (1 vez) e depois chama atendente.
- Acima de `FRETE_DISTANCIA_MAXIMA_KM` → atendente humano; o grupo de avisos mostra o motivo e
  como o endereço foi interpretado no mapa.
- Fixe `LOJA_COORDENADAS` no `.env` para a origem nunca depender da geocodificação.

### Bot confirma ou recusa o sabor errado

- O sabor escrito pela IA é casado com o cardápio em `painel/catalogo.py`
  (`localizar_variante`): nome exato (ignora acento, emoji e maiúsculas), parte do nome
  quando só um sabor combina ("watermelon" → "Strawberry Apple Watermelon") ou erro de
  digitação leve. Na dúvida (ex.: "ice" combina com vários) o cliente recebe a lista.
- Traduções e apelidos ("melancia", "o de cereja") ficam a cargo da IA (regra 16 do
  prompt em `core/llm_service.py`); o código só garante que o resultado existe.

### Cardápio não aparece como lista

- Com a Evolution em modo Baileys, listas/botões interativos frequentemente **não aparecem**
  no celular (a API responde OK mesmo assim). Por isso o padrão é o cardápio em texto
  (`WHATSAPP_MENU_INTERATIVO=false`). Para testar a lista, ative a variável e confira no celular.
- O WhatsApp limita listas a 10 linhas e títulos de 24 caracteres; por isso a lista é
  navegada em níveis: categoria → produto → sabor.

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
