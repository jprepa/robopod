"""Operações da loja usadas pelo bot e pelo painel (clientes, mensagens,
pedidos e estoque)."""

import logging
from datetime import timedelta
from decimal import Decimal

from asgiref.sync import sync_to_async
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from . import catalogo
from .models import Cliente, ItemPedido, Mensagem, Pedido, Produto, Variante

logger = logging.getLogger(__name__)


# ── Clientes e mensagens ──


async def registrar_mensagem_cliente(telefone: str, texto: str, nome_whatsapp: str = '') -> Cliente:
    """Salva a mensagem recebida e cria/atualiza o cliente."""
    agora = timezone.now()
    cliente, criado = await Cliente.objects.aget_or_create(
        telefone=telefone, defaults={'nome_whatsapp': nome_whatsapp[:120]},
    )
    campos = {'ultima_mensagem_em': agora}
    if nome_whatsapp and not criado and cliente.nome_whatsapp != nome_whatsapp:
        campos['nome_whatsapp'] = nome_whatsapp[:120]
    await Cliente.objects.filter(pk=cliente.pk).aupdate(**campos)
    await Mensagem.objects.acreate(
        telefone=telefone, autor=Mensagem.Autor.CLIENTE, texto=texto, criado_em=agora,
    )
    return cliente


async def registrar_mensagem_enviada(telefone: str, texto: str, autor: str, enviada: bool) -> None:
    """Salva uma mensagem enviada pelo bot ou pelo atendente. Nunca levanta erro:
    falhar o histórico não pode impedir o atendimento."""
    try:
        await Mensagem.objects.acreate(telefone=telefone, autor=autor, texto=texto, enviada=enviada)
    except Exception:
        logger.exception('Falha ao registrar mensagem enviada para %s', telefone)


async def atendente_falou_recentemente(telefone: str, minutos: int = 15) -> bool:
    limite = timezone.now() - timedelta(minutes=minutos)
    return await Mensagem.objects.filter(
        telefone=telefone, autor=Mensagem.Autor.ATENDENTE, criado_em__gte=limite,
    ).aexists()


async def aviso_humano_recente(telefone: str, texto_aviso: str, minutos: int = 15) -> bool:
    """True se o bot já mandou esse aviso nos últimos minutos (evita spam)."""
    limite = timezone.now() - timedelta(minutes=minutos)
    return await Mensagem.objects.filter(
        telefone=telefone, autor=Mensagem.Autor.BOT,
        texto=texto_aviso, criado_em__gte=limite,
    ).aexists()


async def cliente_bloqueado(telefone: str) -> bool:
    return await Cliente.objects.filter(telefone=telefone, bloqueado=True).aexists()


# ── Pedidos e estoque ──


def _movimentar_estoque(pedido: Pedido, sinal: int) -> None:
    for item in pedido.itens.all():
        delta = sinal * item.quantidade
        if item.variante_id:
            Variante.objects.filter(pk=item.variante_id, estoque__isnull=False).update(
                estoque=F('estoque') + delta,
            )
        elif item.produto_id:
            Produto.objects.filter(pk=item.produto_id, estoque__isnull=False).update(
                estoque=F('estoque') + delta,
            )
    catalogo.invalidar()  # update() não dispara sinais


def _criar_pedido(telefone: str, carrinho: dict, frete: dict) -> Pedido:
    with transaction.atomic():
        cliente, _ = Cliente.objects.get_or_create(telefone=telefone)
        endereco = carrinho.get('endereco', '')
        if endereco:
            Cliente.objects.filter(pk=cliente.pk).update(ultimo_endereco=endereco[:255])

        itens = carrinho.get('itens', [])
        subtotal = sum(Decimal(str(i['preco_unitario'])) * i['quantidade'] for i in itens)
        valor_frete = Decimal(str(frete.get('valor_frete') or 0))
        distancia = frete.get('distancia_km')
        pedido = Pedido.objects.create(
            cliente=cliente,
            subtotal=subtotal,
            frete=valor_frete,
            total=subtotal + valor_frete,
            endereco=endereco[:255],
            endereco_mapa=(frete.get('endereco_formatado') or '')[:255],
            frete_fornecedor=frete.get('fornecedor', ''),
            distancia_km=Decimal(f'{distancia:.2f}') if distancia is not None else None,
        )
        ItemPedido.objects.bulk_create([
            ItemPedido(
                pedido=pedido,
                produto_id=i.get('produto_id'),
                variante_id=i.get('variante_id'),
                nome=i['nome'][:200],
                quantidade=i['quantidade'],
                preco_unitario=Decimal(str(i['preco_unitario'])),
            )
            for i in itens
        ])
        # Reserva o estoque já na criação; cancelar devolve
        _movimentar_estoque(pedido, -1)
        pedido.estoque_baixado = True
        pedido.save(update_fields=['estoque_baixado'])
    return pedido


async def criar_pedido(telefone: str, carrinho: dict, frete: dict) -> Pedido | None:
    """Cria o pedido (aguardando pagamento) quando o frete é cotado."""
    try:
        return await sync_to_async(_criar_pedido)(telefone, carrinho, frete)
    except Exception:
        logger.exception('Falha ao criar pedido para %s', telefone)
        return None


def mudar_status(pedido: Pedido, status: str) -> None:
    """Muda o status, devolvendo o estoque ao cancelar (e baixando de novo se
    um pedido cancelado for reaberto)."""
    with transaction.atomic():
        pedido = Pedido.objects.select_for_update().get(pk=pedido.pk)
        if status == Pedido.Status.CANCELADO and pedido.estoque_baixado:
            _movimentar_estoque(pedido, +1)
            pedido.estoque_baixado = False
        elif status != Pedido.Status.CANCELADO and not pedido.estoque_baixado:
            _movimentar_estoque(pedido, -1)
            pedido.estoque_baixado = True
        pedido.status = status
        pedido.save(update_fields=['status', 'estoque_baixado', 'atualizado_em'])


async def marcar_comprovante(pedido_id: int | None) -> None:
    if pedido_id:
        await Pedido.objects.filter(
            pk=pedido_id, status=Pedido.Status.AGUARDANDO_PAGAMENTO,
        ).aupdate(status=Pedido.Status.COMPROVANTE_RECEBIDO, atualizado_em=timezone.now())
