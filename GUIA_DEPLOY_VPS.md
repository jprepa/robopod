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

# ── Painel (opcional: endereço público, para os avisos virem com link) ──
PAINEL_URL=

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

    # Webhook, painel (/painel/) e admin — tudo vai para o Django
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
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

## 🔄 ATUALIZAR A VPS — Frete novo, Painel e Avisos no WhatsApp

As mudanças são feitas e testadas **no seu PC**, salvas em commits do Git e enviadas
ao GitHub. A VPS só **baixa** a versão nova do GitHub e reinicia o app. Nada é
editado direto na VPS, a não ser o `.env`.

```
Seu PC ──git push──▶ GitHub ──git pull──▶ VPS ──docker compose──▶ app no ar
```

### Passo 1 — No seu PC: enviar a versão nova para o GitHub

No terminal, dentro de `C:\Users\jppss\Desktop\robopod`:

```bash
# Veja em que branch está e se está tudo commitado (deve dizer "nothing to commit")
git status

# Envie a branch com as mudanças para o GitHub
git push -u origin feat/frete-e-painel
```

Depois, no site do GitHub (github.com/jprepa/robopod), abra o **Pull Request** da
branch `feat/frete-e-painel` para `main`, revise e clique em **Merge**.

> Sem usar Pull Request, dá para juntar direto pelo terminal:
> `git checkout main` → `git merge feat/frete-e-painel` → `git push origin main`

### Passo 2 — Na VPS: entrar e conferir a pasta

```bash
ssh root@SEU_IP
cd /root/robopod      # ou a pasta onde está o docker-compose.yml do Robopod
git status
```

- Se aparecer `On branch main` → a VPS usa Git, siga para o Passo 3.
- Se aparecer `not a git repository` → os arquivos foram copiados com `scp`.
  Veja o quadro **"Se a VPS não usa Git"** no fim desta seção.
- Se o `git status` listar arquivos **modificados** (`modified:`), alguém editou
  direto na VPS. Guarde essas mudanças antes com `git stash` (e avise o dev).
- Uma pasta `core/migrations/` aparecendo como "untracked" é **normal**: não apague.

### Passo 3 — Backup do banco (sempre antes de atualizar)

```bash
docker compose exec -T db pg_dump -U robopod robopod > ~/backup_robopod_$(date +%F_%H%M).sql
ls -lh ~/backup_robopod_*        # confira que o arquivo não está vazio
```

### Passo 4 — Baixar a versão nova

```bash
git pull origin main
git log --oneline -4    # deve mostrar "Avisos da equipe em grupo do WhatsApp..." no topo
```

### Passo 5 — Atualizar o `.env`

```bash
nano .env
```

Adicione no fim (ajuste os valores):

```env
# Coordenadas exatas da loja: no Google Maps, clique com o botão direito na loja
# e clique no número que aparece (ex.: -22.9035, -43.5612). Cole sem espaço:
LOJA_COORDENADAS=-22.9035,-43.5612
# Cidade usada quando o cliente não manda a cidade no endereço
FRETE_CIDADE_PADRAO=Rio de Janeiro - RJ

# Endereço do painel com https (só depois de configurar o domínio — Etapa 9)
CSRF_TRUSTED_ORIGINS=https://bot.seudominio.com.br
PAINEL_URL=https://bot.seudominio.com.br
# Hora em que vira o dia nos relatórios (6 = vendas até 05:59 contam na noite anterior)
PAINEL_VIRADA_DIA_HORA=6
```

Pode apagar a linha `TEAMS_WEBHOOK_URL` (não é mais usada). Salve com `Ctrl+O`, `Enter`, `Ctrl+X`.

### Passo 6 — Reiniciar o app (aplica o código, o `.env` e cria as tabelas novas)

```bash
docker compose up -d --build --force-recreate app
docker compose logs -f app
```

No log deve aparecer algo como:

```
Applying painel.0001_initial... OK
Applying painel.0002_cardapio_inicial... OK
Applying painel.0003_configuracao... OK
```

e depois `Uvicorn running on http://0.0.0.0:8000`. Saia do log com `Ctrl+C` (o app continua rodando).

> A migração `0002_cardapio_inicial` grava no banco o cardápio que estava no código.
> Daí em diante, preços e produtos são editados pelo painel (Estoque).

### Passo 7 — Criar o login do painel

Se você já criou um usuário para o `/admin/`, ele também entra no painel. Senão:

```bash
docker compose exec app python manage.py createsuperuser
```

Para cada pessoa da equipe, crie um usuário próprio (o mesmo comando, ou pelo `/admin/` → Usuários).

### Passo 8 — Testar o frete

```bash
docker compose exec app python manage.py testar_frete "Av joaquim magalhaes, 180"
```

Deve mostrar `"sucesso": true`, a distância e o `endereco_formatado` (onde o endereço foi
localizado). Se der `fora_area`, confira se `LOJA_COORDENADAS` está certo.

### Passo 9 — Abrir o painel

- **Com domínio + SSL (recomendado):** confira se o Nginx está como na Etapa 9.2
  (com `location /`), rode `nginx -t && systemctl reload nginx` e abra
  `https://bot.seudominio.com.br/painel/`.
- **Só para um teste rápido, sem domínio:** `http://SEU_IP:8000/painel/`. Sem HTTPS a
  senha trafega aberta. Não use assim no dia a dia.

Se o login der **erro 403 (CSRF)**, o `CSRF_TRUSTED_ORIGINS` não bate com o endereço
que está no navegador (precisa ter `https://` e nenhuma barra no fim). Corrija o `.env`
e repita o Passo 6.

### Passo 10 — Configurar o grupo de avisos

1. No celular, crie um grupo no WhatsApp com a equipe e **adicione o número da loja**.
2. No painel: **Configurações** → escolha o grupo → **Salvar** → **Enviar mensagem de teste**.
3. A mensagem "Teste do Robopod" deve chegar no grupo.

### Passo 11 — Teste completo pelo WhatsApp

Do seu celular pessoal, faça um pedido até o fim (itens → endereço → comprovante) e confira:

- no WhatsApp: o resumo traz "Localizado como: ..." e o número do pedido;
- no painel: a conversa em **Conversas**, o pedido em **Pedidos** e o cliente em **Clientes**;
- no grupo: os avisos de pedido novo e de comprovante.

### Se algo der errado — voltar para a versão anterior

```bash
cd /root/robopod
git checkout 7ddbdc5            # versão de antes destas mudanças
docker compose up -d --build --force-recreate app
```

As tabelas novas não atrapalham a versão antiga. Para voltar à versão nova depois:
`git checkout main`. Só restaure o backup do Passo 3 se o banco tiver sido danificado:

```bash
docker compose exec -T db psql -U robopod robopod < ~/backup_robopod_DATA.sql
```

### Se a VPS não usa Git (arquivos copiados com scp)

O mais seguro é passar a usar Git, para as próximas atualizações serem só `git pull`:

```bash
cd /root/robopod
cp .env ~/env_backup                 # guarda o .env
git init
git remote add origin https://github.com/jprepa/robopod.git
git fetch origin
git checkout -f -B main origin/main  # traz o código do GitHub (o .env não é tocado)
cp ~/env_backup .env                 # por garantia
```

Depois siga do **Passo 3** em diante. Se o repositório for privado, o `git fetch` pede
usuário e um **token** do GitHub (Settings → Developer settings → Personal access tokens)
no lugar da senha.

> **Segurança (recomendado):** com o Nginx na frente, troque no `docker-compose.yml` a linha
> `- "8000:8000"` por `- "127.0.0.1:8000:8000"`. Assim o painel só abre pelo domínio com
> HTTPS e não fica exposto pelo IP. O webhook da Evolution continua funcionando, porque
> ela fala com o app pela rede interna do Docker.

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
s.save()
print('Resetada!')
"

# Admin do Django
# http://SEU_IP:8000/admin/
```

### Atualizar o cardápio (modelos, preços e sabores)

O cardápio do bot fica no arquivo `cardapio.txt` (raiz do projeto), no mesmo formato
da lista de sabores: `Modelo - 3.500 puffs`, depois `R$ 90,00` e um sabor por linha.
Marque com ❌ no começo da linha o sabor esgotado. Edite o arquivo (no PC, envie pelo
Git; ou direto na VPS com `nano cardapio.txt`) e rode:

```bash
# Mostra o que vai mudar, sem gravar
docker compose exec app python manage.py importar_cardapio --simular

# Aplica
docker compose exec app python manage.py importar_cardapio
```

Vale na hora para o bot, sem reiniciar. Produtos e sabores são atualizados pelo nome
(não duplica); o que não está no arquivo é **ocultado** (não apagado) — use
`--manter-outros` para não mexer nos outros produtos. Sabor sem ❌ que estava zerado
volta a aparecer. Ajustes do dia a dia (+/− estoque, preço) continuam pelo painel.

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
