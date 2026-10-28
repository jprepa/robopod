import os
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# Desligado por padrão: ligue (DEBUG=True) só no PC de desenvolvimento. A tela
# de erro do modo debug mostra variáveis, configurações e dados dos clientes.
DEBUG = os.getenv('DEBUG', 'False').lower() in ('true', '1', 'yes')
SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', '')
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured('Defina DJANGO_SECRET_KEY no .env (veja o GUIA_DEPLOY_VPS.md)')
    SECRET_KEY = 'insecure-dev-key-so-para-desenvolvimento'
# "app" é o nome do serviço no docker-compose: a Evolution chama o webhook por ele
ALLOWED_HOSTS = [h.strip() for h in os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1,app').split(',') if h.strip()]
# Ex.: https://robopod.seu-tailnet.ts.net (necessário para login via HTTPS/domínio)
CSRF_TRUSTED_ORIGINS = [o for o in os.getenv('CSRF_TRUSTED_ORIGINS', '').split(',') if o]

# Painel acessado por HTTPS (Tailscale Serve ou Nginx): cookies só trafegam
# criptografados. O Django recebe HTTP do proxy, então confia no cabeçalho dele.
PAINEL_HTTPS = os.getenv('PAINEL_HTTPS', 'False').lower() in ('true', '1', 'yes')
SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = PAINEL_HTTPS
if PAINEL_HTTPS:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    # Sem redirecionar HTTP -> HTTPS: o webhook da Evolution chega por HTTP na rede do Docker
    SECURE_HSTS_SECONDS = 30 * 24 * 60 * 60
SESSION_COOKIE_HTTPONLY = True
# Login dura um turno: depois de 12 h é preciso entrar de novo
SESSION_COOKIE_AGE = 12 * 60 * 60
X_FRAME_OPTIONS = 'DENY'

# Token que a Evolution manda na URL do webhook (?token=...); sem ele o webhook
# recusa tudo (exceto com DEBUG ligado). Gere com: python -c "import secrets; print(secrets.token_urlsafe(32))"
WEBHOOK_TOKEN = os.getenv('WEBHOOK_TOKEN', '')

# Notificações no celular (Web Push). Gere o par com: python manage.py gerar_chaves_push
# Sem as chaves, o painel só esconde o botão de ativar e o serviço `notificacoes` fica parado.
VAPID_PUBLIC_KEY = os.getenv('VAPID_PUBLIC_KEY', '')
VAPID_PRIVATE_KEY = os.getenv('VAPID_PRIVATE_KEY', '')
# Contato do dono do painel exigido pela Apple/Google (mailto:email ou https://...)
VAPID_CONTATO = os.getenv('VAPID_CONTATO', '') or os.getenv('PAINEL_URL', '')

# Login do painel/admin: bloqueia por 15 min depois de 5 senhas erradas
# seguidas para o mesmo usuário ou IP
LOGIN_TENTATIVAS_MAXIMAS = int(os.getenv('LOGIN_TENTATIVAS_MAXIMAS', '5'))
LOGIN_BLOQUEIO_MINUTOS = int(os.getenv('LOGIN_BLOQUEIO_MINUTOS', '15'))
AUTHENTICATION_BACKENDS = ['painel.autenticacao.BackendComLimiteDeTentativas']

# ── Retenção de dados dos clientes (python manage.py limpar_dados, todo dia) ──
# Endereço dos pedidos entregues/cancelados e o comprovante que tiver sobrado
RETENCAO_ENDERECO_DIAS = int(os.getenv('RETENCAO_ENDERECO_DIAS', '3'))
# Histórico das conversas (painel, contexto da IA e mensagens guardadas pela Evolution)
RETENCAO_MENSAGENS_DIAS = int(os.getenv('RETENCAO_MENSAGENS_DIAS', '3'))

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'core',
    'painel',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    # Formulários e htmx do painel já mandam o token; o webhook é @csrf_exempt
    'django.middleware.csrf.CsrfViewMiddleware',
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
                'painel.contexto.lojas',
                'painel.contexto.perfil',
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
        'PASSWORD': os.getenv('POSTGRES_PASSWORD', ''),
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

# Arquivos enviados pelo painel (PDF do cardápio) e comprovantes. O volume
# `./media` do docker-compose deixa a pasta na VPS e ela sobrevive a rebuilds.
MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

# ── Painel ──
LOGIN_URL = 'painel:login'
LOGIN_REDIRECT_URL = 'painel:inicio'
LOGOUT_REDIRECT_URL = 'painel:login'
# Hora em que vira o "dia comercial" nos relatórios: com a loja aberta das 18h
# às 03h, uma venda à 01h conta para a noite anterior.
PAINEL_VIRADA_DIA_HORA = int(os.getenv('PAINEL_VIRADA_DIA_HORA', '6'))

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ── Chaves de API ──
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')
OPENAI_MODEL = os.getenv('OPENAI_MODEL', 'gpt-4o-mini')
# Transcrição dos áudios dos clientes (whisper-1 também funciona)
OPENAI_TRANSCRICAO_MODEL = os.getenv('OPENAI_TRANSCRICAO_MODEL', 'gpt-4o-mini-transcribe')

# ── Evolution API ──
EVOLUTION_API_URL = os.getenv('EVOLUTION_API_URL', 'http://localhost:8080')
EVOLUTION_API_KEY = os.getenv('EVOLUTION_API_KEY', '')
EVOLUTION_INSTANCE = os.getenv('EVOLUTION_INSTANCE', 'robopod')

# ── Frete ──
ENDERECO_LOJA = os.getenv('ENDERECO_LOJA', 'Rua Exemplo, 123 - Centro, São Paulo - SP')
# 'lalamove' (API, cai na estimativa se falhar), 'estimativa' (distância x tabela)
# ou 'uber_scraper' (exige login na Uber)
FRETE_MODO = os.getenv('FRETE_MODO', 'estimativa').lower()
# Opcional: "lat,lng" da loja; vazio = geocodifica ENDERECO_LOJA
LOJA_COORDENADAS = os.getenv('LOJA_COORDENADAS', '')
# Opcional: com chave usa Google Routes/Geocoding API; sem chave usa OpenStreetMap
GOOGLE_MAPS_API_KEY = os.getenv('GOOGLE_MAPS_API_KEY', '')
LALAMOVE_API_KEY = os.getenv('LALAMOVE_API_KEY', '')
LALAMOVE_API_SECRET = os.getenv('LALAMOVE_API_SECRET', '')
LALAMOVE_SANDBOX = os.getenv('LALAMOVE_SANDBOX', 'true').lower() in ('true', '1', 'yes')
LALAMOVE_SERVICE_TYPE = os.getenv('LALAMOVE_SERVICE_TYPE', 'MOTORCYCLE')
FRETE_TAXA_BASE = float(os.getenv('FRETE_TAXA_BASE', '6.00'))
FRETE_VALOR_KM = float(os.getenv('FRETE_VALOR_KM', '1.50'))
FRETE_VALOR_MINIMO = float(os.getenv('FRETE_VALOR_MINIMO', '9.00'))
FRETE_MARGEM_PERCENTUAL = float(os.getenv('FRETE_MARGEM_PERCENTUAL', '0'))
FRETE_DISTANCIA_MAXIMA_KM = float(os.getenv('FRETE_DISTANCIA_MAXIMA_KM', '20'))
# Cidade anexada a endereços sem cidade ("Rio de Janeiro - RJ"); vazio = fim de ENDERECO_LOJA
FRETE_CIDADE_PADRAO = os.getenv('FRETE_CIDADE_PADRAO', '')
# Frete fixo por bairro (R$). Tem prioridade sobre Lalamove/estimativa; bairros
# fora da lista seguem o FRETE_MODO. Acentos e maiúsculas não importam.
FRETE_POR_BAIRRO = {
    'Barra Olímpica': 10,
    'Barra': 10,
    'Barra da Tijuca': 10,
    'Recreio': 15,
    'Recreio dos Bandeirantes': 15,
    'São Conrado': 15,
    'Leblon': 20,
    'Ipanema': 25,
    'Copacabana': 25,
    'Botafogo': 25,
    'Tijuca': 25,
    'Centro': 30,
}

# ── Cardápio no WhatsApp ──
# Listas/botões interativos muitas vezes NÃO aparecem com a Evolution em modo
# Baileys. Deixe False (texto formatado) até confirmar que funcionam no celular.
WHATSAPP_MENU_INTERATIVO = os.getenv('WHATSAPP_MENU_INTERATIVO', 'False').lower() in ('true', '1', 'yes')
# Opcional: URL pública de uma imagem/arte do cardápio, enviada antes do texto
CARDAPIO_IMAGEM_URL = os.getenv('CARDAPIO_IMAGEM_URL', '')

# ── Avisos à equipe ──
# O grupo do WhatsApp que recebe os avisos é escolhido em Painel > Configurações.
# Endereço público do painel (ex.: https://painel.seudominio.com.br); com ele os
# avisos trazem um link direto para a conversa do cliente.
PAINEL_URL = os.getenv('PAINEL_URL', '')

# ── PIX ──
CHAVE_PIX = os.getenv('CHAVE_PIX', '')
