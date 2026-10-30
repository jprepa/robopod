"""Operações da loja usadas pelo bot e pelo painel (clientes, mensagens,
pedidos e estoque)."""

import logging
from datetime import timedelta
from decimal import Decimal

from asgiref.sync import sync_to_async
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from . import auditoria, catalogo
from .models import Cliente, ItemPedido, Loja, Mensagem, Pedido, Produto, Revendedor, Variante

logger = logging.getLogger(__name__)


# ── Clientes e mensagens ──


def normalizar_telefone(texto: str) -> str | None:
    """'(11) 98888-7777' -> '5511988887777'. Vazio -> ''. Inválido -> None."""
    digitos = ''.join(c for c in texto if c.isdigit())
    if not digitos:
        return ''
    if len(digitos) in (10, 11):
        digitos = '55' + digitos
    return digitos if len(digitos) in (12, 13) and digitos.startswith('55') else None


def chave_telefone(telefone: str) -> str:
    """DDD + 8 últimos dígitos: o WhatsApp às vezes manda celular sem o 9 na
    frente, então '5511988887777' e '551188887777' são o mesmo número."""
    digitos = ''.join(c for c in telefone if c.isdigit())
    if digitos.startswith('55') and len(digitos) in (12, 13):
        return digitos[2:4] + digitos[-8:]
    return digitos


async def registrar_mensagem_cliente(
    telefone: str, texto: str, nome_whatsapp: str = '', loja_id: int | None = None,
    midia: tuple[bytes, str] | None = None,
) -> Cliente:
    """Salva a mensagem recebida e cria/atualiza o cliente. A loja por onde a
    mensagem chegou vira a loja atual do cliente (as respostas saem por ela).

    `midia` (conteúdo, mimetype) é a foto/PDF que o atendente vê no balão.
    """
    agora = timezone.now()
    cliente, criado = await Cliente.objects.aget_or_create(
        telefone=telefone,
        defaults={'nome_whatsapp': nome_whatsapp[:120], 'loja_origem_id': loja_id, 'loja_atual_id': loja_id},
    )
    campos = {'ultima_mensagem_em': agora}
    if nome_whatsapp and not criado and cliente.nome_whatsapp != nome_whatsapp:
        campos['nome_whatsapp'] = nome_whatsapp[:120]
    if loja_id:
        campos['loja_atual_id'] = cliente.loja_atual_id = loja_id
        if cliente.loja_origem_id is None:
            campos['loja_origem_id'] = cliente.loja_origem_id = loja_id
    await Cliente.objects.filter(pk=cliente.pk).aupdate(**campos)
    arquivo = None
    if midia is not None:
        conteudo, mimetype = midia
        # Só tipos que o navegador mostra com segurança (nada de HTML/SVG)
        extensao = _EXTENSOES_MIDIA.get(mimetype)
        if conteudo and extensao:
            arquivo = ContentFile(conteudo, name=f'{telefone}-{agora:%Y%m%d%H%M%S}.{extensao}')
    await Mensagem.objects.acreate(
        telefone=telefone, loja_id=loja_id, autor=Mensagem.Autor.CLIENTE, texto=texto,
        midia=arquivo or '', criado_em=agora,
    )
    return cliente


async def registrar_mensagem_enviada(
    telefone: str, texto: str, autor: str, enviada: bool, loja_id: int | None = None,
) -> None:
    """Salva uma mensagem enviada pelo bot ou pelo atendente. Nunca levanta erro:
    falhar o histórico não pode impedir o atendimento."""
    try:
        await Mensagem.objects.acreate(
            telefone=telefone, loja_id=loja_id, autor=autor, texto=texto, enviada=enviada,
        )
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


async def bot_enviou_desde(telefone: str, texto: str, desde) -> bool:
    """True se o bot já mandou exatamente esse texto ao cliente desde `desde`."""
    return await Mensagem.objects.filter(
        telefone=telefone, autor=Mensagem.Autor.BOT, texto=texto, criado_em__gte=desde,
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
        itens = carrinho.get('itens', [])
        subtotal = sum(Decimal(str(i['preco_unitario'])) * i['quantidade'] for i in itens)
        valor_frete = Decimal(str(frete.get('valor_frete') or 0))
        distancia = frete.get('distancia_km')
        pedido = Pedido.objects.create(
            cliente=cliente,
            loja_id=cliente.loja_atual_id,
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
    auditoria.registrar_bot(
        auditoria.Tipo.PEDIDO, 'pedido_criar',
        f'Pedido #{pedido.pk} criado pelo bot para {cliente.nome_exibicao} '
        f'(total R$ {auditoria.texto(pedido.total)})',
        alvo=pedido,
    )
    return pedido


async def criar_pedido(telefone: str, carrinho: dict, frete: dict) -> Pedido | None:
    """Cria o pedido (aguardando pagamento) quando o frete é cotado."""
    try:
        return await sync_to_async(_criar_pedido)(telefone, carrinho, frete)
    except Exception:
        logger.exception('Falha ao criar pedido para %s', telefone)
        return None


def registrar_venda_manual(
    itens: list[dict], *, status: str, criado_em, telefone: str = '', nome: str = '',
    frete: Decimal = Decimal('0'), observacoes: str = '', vendedor=None,
    origem: str = Pedido.Origem.MANUAL, revendedor: Revendedor | None = None, loja: Loja | None = None,
) -> Pedido:
    """Lança no painel uma venda feita fora do bot (balcão, ligação, conversa
    no WhatsApp pessoal...) e baixa o estoque como num pedido normal.

    Cada item: {'produto': Produto, 'variante': Variante | None,
    'quantidade': int, 'preco_unitario': Decimal}. Venda para `revendedor`
    fica a receber até o acerto. `loja`: de qual loja foi a venda (relatórios)."""
    with transaction.atomic():
        cliente = None
        if telefone:
            cliente, criado = Cliente.objects.get_or_create(telefone=telefone, defaults={'nome': nome[:120]})
            if nome and not criado and not cliente.nome:
                Cliente.objects.filter(pk=cliente.pk).update(nome=nome[:120])

        subtotal = sum(i['preco_unitario'] * i['quantidade'] for i in itens)
        pedido = Pedido.objects.create(
            cliente=cliente,
            origem=origem,
            loja=loja,
            revendedor=revendedor,
            status=status,
            subtotal=subtotal,
            frete=frete,
            total=subtotal + frete,
            observacoes=observacoes,
            vendedor=vendedor,
            criado_em=criado_em,
        )
        ItemPedido.objects.bulk_create([
            ItemPedido(
                pedido=pedido,
                produto=i['produto'],
                variante=i['variante'],
                nome=(f"{i['produto'].nome} - {i['variante'].nome}" if i['variante'] else i['produto'].nome)[:200],
                quantidade=i['quantidade'],
                preco_unitario=i['preco_unitario'],
            )
            for i in itens
        ])
        _movimentar_estoque(pedido, -1)
        pedido.estoque_baixado = True
        pedido.save(update_fields=['estoque_baixado'])
    return pedido


def acertar_revendedor(revendedor: Revendedor) -> tuple[int, Decimal]:
    """Marca como pagas todas as vendas a receber do revendedor.

    Returns:
        (quantidade de vendas, valor acertado)."""
    pendentes = Pedido.objects.filter(revendedor=revendedor, acertado_em__isnull=True).exclude(
        status=Pedido.Status.CANCELADO,
    )
    with transaction.atomic():
        valor = sum((p.total for p in pendentes.select_for_update()), Decimal('0'))
        quantidade = pendentes.update(acertado_em=timezone.now())
    return quantidade, valor


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
        campos = ['status', 'estoque_baixado', 'atualizado_em']
        # Pagamento confirmado ou pedido cancelado: o comprovante já cumpriu o
        # papel, então é apagado (o arquivo só sai do disco depois que a
        # transação for confirmada)
        if status in (*Pedido.STATUS_FATURADOS, Pedido.Status.CANCELADO) and pedido.comprovante:
            arquivo = pedido.comprovante.name
            storage = pedido.comprovante.storage
            pedido.comprovante = ''
            campos.append('comprovante')
            transaction.on_commit(lambda: storage.delete(arquivo))
        pedido.save(update_fields=campos)


async def cancelar_pedido_cliente(pedido_id: int | None) -> None:
    """Cancela (devolvendo o estoque) o pedido que o cliente desistiu ou quis
    alterar antes de mandar o comprovante."""
    if not pedido_id:
        return
    pedido = await Pedido.objects.filter(
        pk=pedido_id, status=Pedido.Status.AGUARDANDO_PAGAMENTO,
    ).afirst()
    if not pedido:
        return
    await sync_to_async(mudar_status)(pedido, Pedido.Status.CANCELADO)
    await auditoria.aregistrar_bot(
        auditoria.Tipo.PEDIDO, 'pedido_status',
        f'Pedido #{pedido_id}: cancelado pelo cliente no bot', alvo=('pedido', pedido_id),
        mudancas={'Status': [
            Pedido.Status.AGUARDANDO_PAGAMENTO.label, Pedido.Status.CANCELADO.label,
        ]},
    )


_EXTENSOES_MIDIA = {
    'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp', 'application/pdf': 'pdf',
}


async def salvar_comprovante(pedido_id: int | None, conteudo: bytes, mimetype: str) -> None:
    """Guarda a imagem/PDF do comprovante no pedido, para o operador conferir no painel."""
    extensao = _EXTENSOES_MIDIA.get(mimetype)
    if not pedido_id or not conteudo or not extensao:
        return
    pedido = await Pedido.objects.filter(pk=pedido_id).afirst()
    if pedido is None:
        return
    if pedido.comprovante:
        await sync_to_async(pedido.comprovante.delete)(save=False)
    # save=False + update: não sobrescreve o status que o bot muda em seguida
    await sync_to_async(pedido.comprovante.save)(
        f'pedido-{pedido_id}.{extensao}', ContentFile(conteudo), save=False,
    )
    await Pedido.objects.filter(pk=pedido_id).aupdate(comprovante=pedido.comprovante.name)


async def marcar_comprovante(pedido_id: int | None) -> None:
    if pedido_id:
        alterados = await Pedido.objects.filter(
            pk=pedido_id, status=Pedido.Status.AGUARDANDO_PAGAMENTO,
        ).aupdate(status=Pedido.Status.COMPROVANTE_RECEBIDO, atualizado_em=timezone.now())
        if alterados:
            await auditoria.aregistrar_bot(
                auditoria.Tipo.PEDIDO, 'pedido_status',
                f'Pedido #{pedido_id}: comprovante recebido pelo bot', alvo=('pedido', pedido_id),
                mudancas={'Status': [
                    Pedido.Status.AGUARDANDO_PAGAMENTO.label, Pedido.Status.COMPROVANTE_RECEBIDO.label,
                ]},
            )
