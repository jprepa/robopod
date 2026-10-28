"""Perfil Operador: usuário do grupo "Operador" usa só Conversas, Estoque (com
venda manual) e Configurações (sem trocar o PDF do cardápio), além de abrir,
fechar e pausar a loja no Início. Superusuário nunca é tratado como operador.
"""

from functools import wraps

from django.contrib import messages
from django.shortcuts import redirect

GRUPO_OPERADOR = 'Operador'


def eh_operador(user) -> bool:
    if not user.is_authenticated or user.is_superuser:
        return False
    if not hasattr(user, '_eh_operador'):
        user._eh_operador = user.groups.filter(name=GRUPO_OPERADOR).exists()
    return user._eh_operador


def fora_do_operador(view):
    """Telas que o operador não acessa: volta para o Início com um aviso."""
    @wraps(view)
    def envolvida(request, *args, **kwargs):
        if eh_operador(request.user):
            messages.error(request, 'Seu acesso não inclui essa tela.')
            return redirect('painel:inicio')
        return view(request, *args, **kwargs)
    return envolvida
