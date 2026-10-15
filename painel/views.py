"""Painel da loja: dashboard, conversas, clientes, pedidos e estoque."""

import logging
from collections import defaultdict
from datetime import datetime, time, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal

from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, F, Max, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.models import SessaoChat
from core.notificacoes import enviar_whatsapp, listar_grupos_whatsapp, notificar_equipe

from . import servicos
from .forms import CategoriaForm, ClienteForm, ProdutoForm, VarianteFormSet
from .models import (
    Categoria, Cliente, Configuracao, ItemPedido, Mensagem, Pedido, Produto, Variante,
)

logger = logging.getLogger(__name__)

_FATURADOS = Pedido.STATUS_FATURADOS
_ABERTOS = (Pedido.Status.AGUARDANDO_PAGAMENTO, Pedido.Status.COMPROVANTE_RECEBIDO)
_EPOCA = datetime(2000, 1, 1, tzinfo=dt_timezone.utc)


# ── Dia comercial (a loja abre à noite e fecha de madrugada) ──


def _dia_comercial(momento: datetime):
    """Data do dia comercial de um instante (01h de sábado conta na sexta)."""
    local = timezone.localtime(momento)
    return (local - timedelta(hours=settings.PAINEL_VIRADA_DIA_HORA)).date()


def _inicio_dia(data) -> datetime:
    return timezone.make_aware(
        datetime.combine(data, time(hour=settings.PAINEL_VIRADA_DIA_HORA)),
    )


def _somar(qs) -> dict:
    r = qs.aggregate(
        produtos=Coalesce(Sum('subtotal'), Decimal('0')),
        frete=Coalesce(Sum('frete'), Decimal('0')),
        pedidos=Count('id'),
    )
    r['total'] = r['produtos'] + r['frete']
    r['ticket'] = r['total'] / r['pedidos'] if r['pedidos'] else Decimal('0')
    return r


# ── Contadores do menu (badge) ──


def _contadores() -> dict:
    return {
        'aguardando_atendente': SessaoChat.objects.filter(
            estado_atual=SessaoChat.Estado.ATENDIMENTO_HUMANO,
        ).count(),
        'pedidos_conferir': Pedido.objects.filter(
            status=Pedido.Status.COMPROVANTE_RECEBIDO,
        ).count(),
    }


def _marcas_alerta() -> dict:
    """Marcadores que só crescem quando algo novo acontece. O navegador guarda
    os últimos valores vistos e toca um som quando algum deles aumenta."""
    def instante(qs) -> float:
        ultimo = qs.aggregate(m=Max('atualizado_em'))['m']
        return ultimo.timestamp() if ultimo else 0

    msgs_cliente = Mensagem.objects.filter(autor=Mensagem.Autor.CLIENTE)
    em_humano = SessaoChat.objects.filter(
        estado_atual=SessaoChat.Estado.ATENDIMENTO_HUMANO,
    ).values('telefone')
    return {
        'mensagem': msgs_cliente.aggregate(m=Max('id'))['m'] or 0,
        'mensagem_humano': msgs_cliente.filter(telefone__in=em_humano).aggregate(m=Max('id'))['m'] or 0,
        'pedido': Pedido.objects.aggregate(m=Max('id'))['m'] or 0,
        'comprovante': instante(Pedido.objects.filter(status=Pedido.Status.COMPROVANTE_RECEBIDO)),
        'pago': instante(Pedido.objects.filter(status=Pedido.Status.PAGO)),
    }


@login_required
def contadores(request):
    return JsonResponse({**_contadores(), 'marcas': _marcas_alerta()})


def _render(request, template: str, contexto: dict) -> HttpResponse:
    return render(request, template, {**contexto, 'contadores': _contadores()})


# ── Dashboard ──


@login_required
def dashboard(request):
    try:
        dias = int(request.GET.get('dias', 30))
    except ValueError:
        dias = 30
    dias = dias if dias in (7, 30, 90) else 30

    agora = timezone.now()
    hoje = _dia_comercial(agora)
    faturados = Pedido.objects.filter(status__in=_FATURADOS)

    resumo_hoje = _somar(faturados.filter(criado_em__gte=_inicio_dia(hoje)))
    resumo_periodo = _somar(faturados.filter(criado_em__gte=_inicio_dia(hoje - timedelta(days=dias - 1))))
    resumo_anterior = _somar(faturados.filter(
        criado_em__gte=_inicio_dia(hoje - timedelta(days=2 * dias - 1)),
        criado_em__lt=_inicio_dia(hoje - timedelta(days=dias - 1)),
    ))
    variacao = None
    if resumo_anterior['total']:
        variacao = float((resumo_periodo['total'] - resumo_anterior['total']) / resumo_anterior['total'] * 100)

    # Série diária (agrupada em Python pelo dia comercial; o volume é pequeno)
    inicio_serie = _inicio_dia(hoje - timedelta(days=dias - 1))
    por_dia = defaultdict(lambda: {'produtos': Decimal('0'), 'frete': Decimal('0'), 'pedidos': 0})
    for criado_em, subtotal, frete in faturados.filter(criado_em__gte=inicio_serie).values_list(
        'criado_em', 'subtotal', 'frete',
    ):
        dia = por_dia[_dia_comercial(criado_em)]
        dia['produtos'] += subtotal
        dia['frete'] += frete
        dia['pedidos'] += 1
    serie = []
    for i in range(dias - 1, -1, -1):
        data = hoje - timedelta(days=i)
        d = por_dia[data]
        serie.append({
            'data': data.isoformat(),
            'rotulo': data.strftime('%d/%m'),
            'produtos': float(d['produtos']),
            'frete': float(d['frete']),
            'total': float(d['produtos'] + d['frete']),
            'pedidos': d['pedidos'],
        })

    top_produtos = (
        ItemPedido.objects
        .filter(pedido__status__in=_FATURADOS, pedido__criado_em__gte=inicio_serie)
        .values('nome')
        .annotate(qtd=Sum('quantidade'), receita=Sum(F('quantidade') * F('preco_unitario')))
        .order_by('-receita')[:6]
    )

    estoque_baixo = [
        *[
            {'nome': p.nome, 'estoque': p.estoque, 'pk': p.pk}
            for p in Produto.objects.filter(
                ativo=True, estoque__isnull=False, estoque__lte=F('estoque_minimo'),
            ).exclude(variantes__ativo=True)
        ],
        *[
            {'nome': str(v), 'estoque': v.estoque, 'pk': v.produto_id}
            for v in Variante.objects.select_related('produto').filter(
                ativo=True, produto__ativo=True, estoque__isnull=False,
                estoque__lte=F('produto__estoque_minimo'),
            )
        ],
    ]
    estoque_baixo.sort(key=lambda e: e['estoque'])

    novos_clientes = Cliente.objects.filter(criado_em__gte=inicio_serie).count()

    return _render(request, 'painel/dashboard.html', {
        'secao': 'dashboard',
        'dias': dias,
        'hoje': resumo_hoje,
        'periodo': resumo_periodo,
        'variacao': variacao,
        'serie': serie,
        'top_produtos': top_produtos,
        'estoque_baixo': estoque_baixo[:8],
        'novos_clientes': novos_clientes,
        'pedidos_abertos': Pedido.objects.filter(status__in=_ABERTOS).count(),
        'virada': settings.PAINEL_VIRADA_DIA_HORA,
    })


# ── Conversas ──


def _conversas_qs(filtro: str, busca: str):
    ultima = Mensagem.objects.filter(telefone=OuterRef('telefone')).order_by('-criado_em')
    estado = SessaoChat.objects.filter(telefone=OuterRef('telefone')).values('estado_atual')[:1]
    # Mensagens do cliente depois da última vez que a conversa foi aberta
    nao_lidas = (
        Mensagem.objects
        .filter(
            telefone=OuterRef('telefone'),
            autor=Mensagem.Autor.CLIENTE,
            criado_em__gt=Coalesce(OuterRef('conversa_lida_em'), Value(_EPOCA)),
        )
        .order_by().values('telefone').annotate(n=Count('id')).values('n')
    )
    qs = (
        Cliente.objects.filter(ultima_mensagem_em__isnull=False)
        .annotate(
            ultima_texto=Subquery(ultima.values('texto')[:1]),
            ultima_autor=Subquery(ultima.values('autor')[:1]),
            estado=Subquery(estado),
            nao_lidas=Coalesce(Subquery(nao_lidas), 0),
        )
        .order_by('-ultima_mensagem_em')
    )
    if filtro == 'atendente':
        qs = qs.filter(estado=SessaoChat.Estado.ATENDIMENTO_HUMANO)
    elif filtro == 'nao_lidas':
        qs = qs.filter(nao_lidas__gt=0)
    elif filtro == 'pedido':
        qs = qs.filter(pedidos__status__in=_ABERTOS).distinct()
    if busca:
        digitos = ''.join(c for c in busca if c.isdigit())
        cond = Q(nome__icontains=busca) | Q(nome_whatsapp__icontains=busca)
        if digitos:
            cond |= Q(telefone__contains=digitos)
        qs = qs.filter(cond)
    return qs


@login_required
def conversas(request):
    filtro = request.GET.get('filtro', '')
    busca = request.GET.get('q', '').strip()
    contexto = {
        'secao': 'conversas',
        'filtro': filtro,
        'busca': busca,
        'conversas': _conversas_qs(filtro, busca)[:100],
        'estados': dict(SessaoChat.Estado.choices),
    }
    if request.headers.get('HX-Request'):
        return render(request, 'painel/parciais/lista_conversas.html', contexto)
    return _render(request, 'painel/conversas.html', contexto)


def _mensagens_html(request, mensagens) -> str:
    return render_to_string('painel/parciais/mensagens.html', {'mensagens': mensagens}, request)


@login_required
def conversa(request, telefone: str):
    cliente = get_object_or_404(Cliente, telefone=telefone)
    sessao = SessaoChat.objects.filter(telefone=telefone).first()

    # Polling: só as mensagens novas depois do último id exibido
    if 'depois' in request.GET:
        try:
            depois = int(request.GET['depois'])
        except ValueError:
            depois = 0
        novas = list(Mensagem.objects.filter(telefone=telefone, pk__gt=depois).order_by('pk'))
        if novas:
            Cliente.objects.filter(pk=cliente.pk).update(conversa_lida_em=timezone.now())
        return JsonResponse({
            'html': _mensagens_html(request, novas),
            'ultimo_id': novas[-1].pk if novas else depois,
            'estado': sessao.get_estado_atual_display() if sessao else '',
        })

    mensagens = list(Mensagem.objects.filter(telefone=telefone).order_by('-pk')[:300])[::-1]
    Cliente.objects.filter(pk=cliente.pk).update(conversa_lida_em=timezone.now())
    return _render(request, 'painel/conversa.html', {
        'secao': 'conversas',
        'cliente': cliente,
        'sessao': sessao,
        'mensagens': mensagens,
        'ultimo_id': mensagens[-1].pk if mensagens else 0,
        'pedidos_abertos': cliente.pedidos.filter(status__in=(*_ABERTOS, Pedido.Status.PAGO, Pedido.Status.EM_ENTREGA)),
        'em_atendimento_humano': bool(sessao and sessao.estado_atual == SessaoChat.Estado.ATENDIMENTO_HUMANO),
        'chave_pix': settings.CHAVE_PIX,
    })


def _sessao(telefone: str) -> SessaoChat:
    sessao, _ = SessaoChat.objects.get_or_create(telefone=telefone)
    return sessao


@login_required
@require_POST
def conversa_responder(request, telefone: str):
    texto = request.POST.get('texto', '').strip()
    if texto:
        sessao = _sessao(telefone)
        # Atendente respondeu: o bot para de responder até devolverem a conversa
        if sessao.estado_atual != SessaoChat.Estado.ATENDIMENTO_HUMANO:
            sessao.estado_atual = SessaoChat.Estado.ATENDIMENTO_HUMANO
            sessao.ultima_interacao = timezone.now()
            sessao.save(update_fields=['estado_atual', 'ultima_interacao'])
        ok = async_to_sync(enviar_whatsapp)(telefone, texto, 'atendente')
        if not ok:
            if request.headers.get('HX-Request'):
                return HttpResponse(status=502)
            messages.error(request, 'Não foi possível enviar pelo WhatsApp (Evolution). Tente de novo.')
    if request.headers.get('HX-Request'):
        return HttpResponse(status=204)
    return redirect('painel:conversa', telefone=telefone)


@login_required
@require_POST
def conversa_assumir(request, telefone: str):
    sessao = _sessao(telefone)
    sessao.estado_atual = SessaoChat.Estado.ATENDIMENTO_HUMANO
    sessao.ultima_interacao = timezone.now()
    sessao.save(update_fields=['estado_atual', 'ultima_interacao'])
    messages.success(request, 'Você assumiu a conversa. O bot não responde até você devolver.')
    return redirect('painel:conversa', telefone=telefone)


@login_required
@require_POST
def conversa_devolver(request, telefone: str):
    sessao = _sessao(telefone)
    sessao.resetar_sessao()
    sessao.save()
    if request.POST.get('avisar'):
        async_to_sync(enviar_whatsapp)(
            telefone, '🤖 Voltamos ao atendimento automático! Como posso te ajudar?',
        )
    messages.success(request, 'Conversa devolvida ao bot.')
    return redirect('painel:conversa', telefone=telefone)


# ── Clientes ──


@login_required
def clientes(request):
    busca = request.GET.get('q', '').strip()
    ordem = request.GET.get('ordem', 'recentes')
    qs = Cliente.objects.annotate(
        num_pedidos=Count('pedidos', filter=Q(pedidos__status__in=_FATURADOS)),
        total_gasto=Coalesce(Sum('pedidos__total', filter=Q(pedidos__status__in=_FATURADOS)), Decimal('0')),
        ultimo_pedido=Max('pedidos__criado_em', filter=Q(pedidos__status__in=_FATURADOS)),
    )
    if busca:
        digitos = ''.join(c for c in busca if c.isdigit())
        cond = Q(nome__icontains=busca) | Q(nome_whatsapp__icontains=busca)
        if digitos:
            cond |= Q(telefone__contains=digitos)
        qs = qs.filter(cond)
    if request.GET.get('bloqueados'):
        qs = qs.filter(bloqueado=True)
    ordenacoes = {
        'recentes': [F('ultima_mensagem_em').desc(nulls_last=True)],
        'gasto': ['-total_gasto'],
        'pedidos': ['-num_pedidos'],
        'nome': ['nome', 'nome_whatsapp'],
    }
    qs = qs.order_by(*ordenacoes.get(ordem, ordenacoes['recentes']))
    pagina = Paginator(qs, 50).get_page(request.GET.get('pagina'))
    return _render(request, 'painel/clientes.html', {
        'secao': 'clientes', 'pagina': pagina, 'busca': busca, 'ordem': ordem,
        'total_clientes': Cliente.objects.count(),
    })


@login_required
def cliente(request, pk: int):
    obj = get_object_or_404(Cliente, pk=pk)
    if request.method == 'POST':
        form = ClienteForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            messages.success(request, 'Cliente atualizado.')
            return redirect('painel:cliente', pk=obj.pk)
    else:
        form = ClienteForm(instance=obj)

    pedidos = obj.pedidos.prefetch_related('itens').all()[:50]
    faturados = obj.pedidos.filter(status__in=_FATURADOS)
    resumo = _somar(faturados)
    favoritos = (
        ItemPedido.objects.filter(pedido__cliente=obj, pedido__status__in=_FATURADOS)
        .values('nome').annotate(qtd=Sum('quantidade')).order_by('-qtd')[:5]
    )
    return _render(request, 'painel/cliente.html', {
        'secao': 'clientes', 'cliente': obj, 'form': form, 'pedidos': pedidos,
        'resumo': resumo, 'favoritos': favoritos,
        'primeiro_pedido': faturados.order_by('criado_em').values_list('criado_em', flat=True).first(),
        'num_mensagens': Mensagem.objects.filter(telefone=obj.telefone).count(),
    })


# ── Pedidos ──

# Mensagem enviada ao cliente quando o status muda (se "avisar cliente" marcado)
_AVISOS_STATUS = {
    Pedido.Status.PAGO: '✅ Pagamento confirmado! Seu pedido #{n} já está sendo preparado.',
    Pedido.Status.EM_ENTREGA: '🛵 Seu pedido #{n} saiu para entrega! Já já chega aí.',
    Pedido.Status.ENTREGUE: '📦 Pedido #{n} entregue. Obrigado pela preferência! 💜',
    Pedido.Status.CANCELADO: '❌ Seu pedido #{n} foi cancelado. Qualquer dúvida, é só chamar.',
}

# Próximo passo sugerido em cada status (botão principal na lista)
_PROXIMO_STATUS = {
    Pedido.Status.AGUARDANDO_PAGAMENTO: Pedido.Status.PAGO,
    Pedido.Status.COMPROVANTE_RECEBIDO: Pedido.Status.PAGO,
    Pedido.Status.PAGO: Pedido.Status.EM_ENTREGA,
    Pedido.Status.EM_ENTREGA: Pedido.Status.ENTREGUE,
}


@login_required
def pedidos(request):
    status = request.GET.get('status', '')
    busca = request.GET.get('q', '').strip()
    qs = Pedido.objects.select_related('cliente').prefetch_related('itens')
    if status == 'abertos':
        qs = qs.exclude(status__in=(Pedido.Status.ENTREGUE, Pedido.Status.CANCELADO))
    elif status:
        qs = qs.filter(status=status)
    if busca:
        digitos = ''.join(c for c in busca if c.isdigit())
        cond = Q(cliente__nome__icontains=busca) | Q(cliente__nome_whatsapp__icontains=busca)
        if digitos:
            cond |= Q(cliente__telefone__contains=digitos) | Q(pk=int(digitos[:9]))
        qs = qs.filter(cond)
    pagina = Paginator(qs, 40).get_page(request.GET.get('pagina'))
    for p in pagina:
        p.proximo = _PROXIMO_STATUS.get(p.status)
        p.proximo_rotulo = Pedido.Status(p.proximo).label if p.proximo else ''
    contagem = dict(Pedido.objects.order_by().values_list('status').annotate(n=Count('id')))
    return _render(request, 'painel/pedidos.html', {
        'secao': 'pedidos', 'pagina': pagina, 'status': status, 'busca': busca,
        'status_opcoes': [(v, r, contagem.get(v, 0)) for v, r in Pedido.Status.choices],
    })


@login_required
def pedido(request, pk: int):
    obj = get_object_or_404(Pedido.objects.select_related('cliente').prefetch_related('itens'), pk=pk)
    obj.proximo = _PROXIMO_STATUS.get(obj.status)
    obj.proximo_rotulo = Pedido.Status(obj.proximo).label if obj.proximo else ''
    return _render(request, 'painel/pedido.html', {
        'secao': 'pedidos', 'pedido': obj, 'status_choices': Pedido.Status.choices,
    })


@login_required
@require_POST
def pedido_status(request, pk: int):
    obj = get_object_or_404(Pedido.objects.select_related('cliente'), pk=pk)
    novo = request.POST.get('status', '')
    if novo not in Pedido.Status.values or novo == obj.status:
        return redirect(request.POST.get('voltar') or 'painel:pedidos')

    servicos.mudar_status(obj, novo)
    rotulo = Pedido.Status(novo).label
    if request.POST.get('avisar') and novo in _AVISOS_STATUS:
        ok = async_to_sync(enviar_whatsapp)(obj.cliente.telefone, _AVISOS_STATUS[novo].format(n=obj.pk))
        messages.success(request, f'Pedido #{obj.pk}: {rotulo}' + (' — cliente avisado.' if ok else ' — falha ao avisar no WhatsApp.'))
    else:
        messages.success(request, f'Pedido #{obj.pk}: {rotulo}.')

    # Pedido encerrado: a conversa volta para o bot
    if novo in (Pedido.Status.ENTREGUE, Pedido.Status.CANCELADO):
        sessao = SessaoChat.objects.filter(telefone=obj.cliente.telefone).first()
        if sessao and sessao.carrinho_temporario.get('pedido_id') == obj.pk:
            sessao.resetar_sessao()
            sessao.save()
    return redirect(request.POST.get('voltar') or 'painel:pedidos')


# ── Estoque ──


@login_required
def estoque(request):
    busca = request.GET.get('q', '').strip()
    produtos = Produto.objects.select_related('categoria').prefetch_related('variantes')
    if busca:
        produtos = produtos.filter(Q(nome__icontains=busca) | Q(variantes__nome__icontains=busca)).distinct()
    if request.GET.get('baixo'):
        produtos = produtos.filter(
            Q(estoque__isnull=False, estoque__lte=F('estoque_minimo'))
            | Q(variantes__estoque__isnull=False, variantes__estoque__lte=F('estoque_minimo')),
        ).distinct()
    grupos = defaultdict(list)
    for p in produtos:
        grupos[p.categoria].append(p)
    return _render(request, 'painel/estoque.html', {
        'secao': 'estoque',
        'grupos': sorted(grupos.items(), key=lambda g: (g[0].ordem, g[0].nome)),
        'busca': busca,
        'baixo': bool(request.GET.get('baixo')),
    })


@login_required
@require_POST
def estoque_ajustar(request, tipo: str, pk: int):
    """Edição rápida (HTMX) de preço, estoque e ativo de um produto ou opção."""
    modelo = Produto if tipo == 'produto' else Variante
    obj = get_object_or_404(modelo, pk=pk)
    campos = []

    if 'delta' in request.POST and obj.estoque is not None:
        obj.estoque = max(0, obj.estoque + int(request.POST['delta']))
        campos.append('estoque')
    if 'estoque' in request.POST:
        valor = request.POST['estoque'].strip()
        obj.estoque = max(0, int(valor)) if valor.lstrip('-').isdigit() else None
        campos.append('estoque')
    if 'preco' in request.POST and tipo == 'produto':
        try:
            obj.preco = Decimal(request.POST['preco'].replace(',', '.'))
            campos.append('preco')
        except ArithmeticError:
            pass
    if 'alternar_ativo' in request.POST:
        obj.ativo = not obj.ativo
        campos.append('ativo')
    if campos:
        obj.save(update_fields=list(dict.fromkeys(campos)))

    produto = obj if tipo == 'produto' else obj.produto
    produto = Produto.objects.prefetch_related('variantes').get(pk=produto.pk)
    return render(request, 'painel/parciais/estoque_produto.html', {'p': produto})


@login_required
def produto_editar(request, pk: int | None = None):
    obj = get_object_or_404(Produto, pk=pk) if pk else None
    if request.method == 'POST':
        if 'excluir' in request.POST and obj:
            nome = obj.nome
            obj.delete()
            messages.success(request, f'Produto "{nome}" excluído.')
            return redirect('painel:estoque')
        form = ProdutoForm(request.POST, instance=obj)
        formset = VarianteFormSet(request.POST, instance=obj or Produto())
        if form.is_valid() and formset.is_valid():
            produto = form.save()
            formset.instance = produto
            formset.save()
            messages.success(request, f'Produto "{produto.nome}" salvo.')
            return redirect('painel:estoque')
    else:
        form = ProdutoForm(instance=obj)
        formset = VarianteFormSet(instance=obj or Produto())
    return _render(request, 'painel/produto_form.html', {
        'secao': 'estoque', 'form': form, 'formset': formset, 'produto': obj,
        'sem_categoria': not Categoria.objects.exists(),
    })


@login_required
def configuracoes(request):
    config = Configuracao.obter()
    if request.method == 'POST':
        if 'testar' in request.POST:
            ok = async_to_sync(notificar_equipe)(
                '✅ *Teste do Robopod*\nOs avisos da loja vão chegar neste grupo.',
            )
            if ok:
                messages.success(request, 'Mensagem de teste enviada ao grupo.')
            else:
                messages.error(request, 'Não foi possível enviar: confira o grupo escolhido e a conexão da Evolution.')
            return redirect('painel:configuracoes')

        grupo = request.POST.get('grupo', '')
        manual = request.POST.get('grupo_manual', '').strip()
        if manual:
            config.grupo_avisos_id, config.grupo_avisos_nome = manual, manual
        elif grupo:
            config.grupo_avisos_id, _, config.grupo_avisos_nome = grupo.partition('||')
        else:
            config.grupo_avisos_id = config.grupo_avisos_nome = ''
        config.avisos_ativos = bool(request.POST.get('avisos_ativos'))
        config.avisar_mensagens_atendimento = bool(request.POST.get('avisar_mensagens_atendimento'))
        config.save()
        messages.success(request, 'Configurações salvas.')
        return redirect('painel:configuracoes')

    grupos, erro_grupos = [], ''
    try:
        grupos = async_to_sync(listar_grupos_whatsapp)()
    except Exception as e:
        logger.warning('Não foi possível listar os grupos do WhatsApp: %s', e)
        erro_grupos = 'Não foi possível buscar os grupos na Evolution. Confira se o WhatsApp está conectado.'
    return _render(request, 'painel/configuracoes.html', {
        'secao': 'configuracoes', 'config': config, 'grupos': grupos, 'erro_grupos': erro_grupos,
        'grupo_na_lista': any(g['id'] == config.grupo_avisos_id for g in grupos),
        'painel_url': settings.PAINEL_URL,
    })


@login_required
def categorias(request):
    editar = request.GET.get('editar')
    obj = Categoria.objects.filter(pk=editar).first() if editar else None
    if request.method == 'POST':
        if 'excluir' in request.POST:
            cat = get_object_or_404(Categoria, pk=request.POST['excluir'])
            if cat.produtos.exists():
                messages.error(request, 'Essa categoria tem produtos. Mova ou exclua os produtos antes.')
            else:
                cat.delete()
                messages.success(request, 'Categoria excluída.')
            return redirect('painel:categorias')
        form = CategoriaForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            messages.success(request, 'Categoria salva.')
            return redirect('painel:categorias')
    else:
        form = CategoriaForm(instance=obj)
    return _render(request, 'painel/categorias.html', {
        'secao': 'estoque', 'form': form, 'editando': obj,
        'categorias': Categoria.objects.annotate(num_produtos=Count('produtos')),
    })
