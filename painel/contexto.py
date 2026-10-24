"""Variáveis disponíveis em todos os templates do painel."""

from django.utils.functional import SimpleLazyObject

from .models import Loja


def lojas(request):
    """Lojas ativas: filtro e etiqueta da loja só aparecem com mais de uma."""
    return {'lojas_ativas': SimpleLazyObject(lambda: list(Loja.objects.filter(ativa=True)))}
