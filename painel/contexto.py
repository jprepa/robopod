"""Variáveis disponíveis em todos os templates do painel."""

from django.utils.functional import SimpleLazyObject

from .models import Loja
from .permissoes import eh_operador


def lojas(request):
    """Lojas ativas: filtro e etiqueta da loja só aparecem com mais de uma."""
    return {'lojas_ativas': SimpleLazyObject(lambda: list(Loja.objects.filter(ativa=True)))}


def perfil(request):
    """`operador`: esconde do menu e das telas o que o perfil Operador não usa."""
    return {'operador': SimpleLazyObject(lambda: eh_operador(request.user))}
