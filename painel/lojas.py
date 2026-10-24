"""Qual número da operação (instância da Evolution) fala com cada cliente.

O cliente é um só para todas as lojas: a resposta sai pelo número por onde ele
mandou a última mensagem (`Cliente.loja_atual`). Grupos e clientes sem loja
usam a loja principal.
"""

from django.conf import settings

from .models import Loja


async def principal() -> Loja | None:
    return await Loja.objects.filter(ativa=True).order_by('-principal', 'pk').afirst()


def principal_sync() -> Loja | None:
    return Loja.objects.filter(ativa=True).order_by('-principal', 'pk').first()


async def da_instancia(nome: str) -> Loja | None:
    """Loja de uma mensagem recebida. Sem o nome da instância no webhook
    (teste manual), vale a principal."""
    if not nome:
        return await principal()
    return await Loja.objects.filter(instancia=nome).afirst()


async def rota(telefone: str) -> tuple[str, int | None]:
    """(instância, id da loja) para mandar mensagem a este número."""
    loja = None
    if not telefone.endswith('@g.us'):
        loja = await Loja.objects.filter(clientes__telefone=telefone, ativa=True).afirst()
    loja = loja or await principal()
    if loja is None:  # nenhuma loja cadastrada: o número do .env
        return settings.EVOLUTION_INSTANCE, None
    return loja.instancia, loja.pk


async def instancia_principal() -> str:
    loja = await principal()
    return loja.instancia if loja else settings.EVOLUTION_INSTANCE
