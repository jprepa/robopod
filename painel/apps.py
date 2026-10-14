from django.apps import AppConfig


class PainelConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'painel'
    verbose_name = 'Painel da loja'

    def ready(self):
        from . import catalogo  # noqa: F401 — conecta os sinais de invalidação
