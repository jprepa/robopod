from django.apps import AppConfig


class PainelConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'painel'
    verbose_name = 'Painel da loja'

    def ready(self):
        from . import auditoria, catalogo  # noqa: F401 — conectam os sinais (login e invalidação)
