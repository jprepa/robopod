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

# ── Cardápio no WhatsApp ──
# Listas/botões interativos muitas vezes NÃO aparecem com a Evolution em modo
# Baileys. Deixe False (texto formatado) até confirmar que funcionam no celular.
WHATSAPP_MENU_INTERATIVO = os.getenv('WHATSAPP_MENU_INTERATIVO', 'False').lower() in ('true', '1', 'yes')
# Opcional: URL pública de uma imagem/arte do cardápio, enviada antes do texto
CARDAPIO_IMAGEM_URL = os.getenv('CARDAPIO_IMAGEM_URL', '')

# ── Teams Webhook ──
TEAMS_WEBHOOK_URL = os.getenv('TEAMS_WEBHOOK_URL', '')

# ── PIX ──
CHAVE_PIX = os.getenv('CHAVE_PIX', '')
