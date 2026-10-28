"""Painel da loja: dashboard, conversas, clientes, pedidos e estoque."""

import logging
import mimetypes
import os
import re
from collections import defaultdict
from datetime import datetime, time, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal

from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, F, Max, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from core.llm_service import formatar_preco, gerar_sabores_texto
from core.models import SessaoChat
from core.notificacoes import (
    criar_instancia, enviar_whatsapp, estado_conexao, listar_grupos_whatsapp, notificar_equipe, qr_code,
)

from . import auditoria, catalogo, push, servicos
from .auditoria import Tipo
from .forms import CategoriaForm, ClienteForm, ProdutoForm, RespostaRapidaForm, VarianteFormSet
from .models import (
    MENSAGEM_FECHADO_PADRAO, Categoria, Cliente, Configuracao, ItemPedido, Loja, Mensagem, Pedido, Produto,
    RegistroAuditoria,
    RespostaRapida, Variante,
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
    config = Configuracao.obter()
    return {
        'aguardando_atendente': SessaoChat.objects.filter(
            estado_atual=SessaoChat.Estado.ATENDIMENTO_HUMANO,
        ).count(),
        'pedidos_conferir': Pedido.objects.filter(
            status=Pedido.Status.COMPROVANTE_RECEBIDO,
        ).count(),
        **{c: getattr(config, c) for c in ('expediente_aberto', 'bot_pausado')},
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
    # Vendas lançadas à mão no painel não tocam som: quem lançou já sabe
    pedidos_bot = Pedido.objects.exclude(origem=Pedido.Origem.MANUAL)
    return {
        'mensagem': msgs_cliente.aggregate(m=Max('id'))['m'] or 0,
        'mensagem_humano': msgs_cliente.filter(telefone__in=em_humano).aggregate(m=Max('id'))['m'] or 0,
        'pedido': pedidos_bot.aggregate(m=Max('id'))['m'] or 0,
        'comprovante': instante(pedidos_bot.filter(status=Pedido.Status.COMPROVANTE_RECEBIDO)),
        'pago': instante(pedidos_bot.filter(status=Pedido.Status.PAGO)),
    }


@login_required
def contadores(request):
    return JsonResponse({**_contadores(), 'marcas': _marcas_alerta()})


def _render(request, template: str, contexto: dict) -> HttpResponse:
    return render(request, template, {**contexto, 'contadores': _contadores()})


# ── Início: o que precisa de atenção agora + expediente ──

# Pedido sem pagamento há mais que isso vira alerta (cliente pode ter desistido)
_ALERTA_PAGAMENTO_APOS = timedelta(minutes=30)
# Depois de abrir a loja, a lista de quem chamou com ela fechada fica visível por esse tempo
_RETORNO_FECHADO_POR = timedelta(hours=12)
_STATUS_ANDAMENTO = (*_ABERTOS, Pedido.Status.PAGO, Pedido.Status.EM_ENTREGA)


def _saudacao(agora: datetime) -> str:
    hora = timezone.localtime(agora).hour
    if 5 <= hora < 12:
        return 'Bom dia'
    return 'Boa tarde' if 12 <= hora < 18 else 'Boa noite'


def _clientes_por_telefone(telefones) -> dict:
    return {c.telefone: c for c in Cliente.objects.filter(telefone__in=list(telefones))}


def _aguardando_atendente() -> list[dict]:
    """Conversas com atendente humano, quem está sem resposta primeiro."""
    sessoes = list(SessaoChat.objects.filter(estado_atual=SessaoChat.Estado.ATENDIMENTO_HUMANO))
    clientes = _clientes_por_telefone(s.telefone for s in sessoes)
    # Última mensagem do cliente e do atendente (o aviso automático do bot não conta como resposta)
    ultimas = (
        Mensagem.objects.filter(
            telefone__in=[s.telefone for s in sessoes],
            autor__in=(Mensagem.Autor.CLIENTE, Mensagem.Autor.ATENDENTE),
        )
        .values('telefone', 'autor').annotate(m=Max('criado_em'))
    )
    ultima = {(u['telefone'], u['autor']): u['m'] for u in ultimas}
    itens = []
    for s in sessoes:
        do_cliente = ultima.get((s.telefone, Mensagem.Autor.CLIENTE))
        do_atendente = ultima.get((s.telefone, Mensagem.Autor.ATENDENTE))
        sem_resposta = do_atendente is None or (do_cliente is not None and do_cliente > do_atendente)
        cliente = clientes.get(s.telefone)
        itens.append({
            'telefone': s.telefone,
            'nome': cliente.nome_exibicao if cliente else s.telefone,
            'sem_resposta': sem_resposta,
            # Ninguém respondeu ainda: espera desde a transferência; senão, desde a última mensagem dele
            'desde': do_cliente if do_atendente and sem_resposta else s.ultima_interacao,
        })
    itens.sort(key=lambda i: (not i['sem_resposta'], i['desde']))
    return itens


def _chamaram_fechado(config: Configuracao, agora: datetime) -> list[dict]:
    """Clientes que mandaram mensagem com a loja fechada. Com a loja aberta de
    novo, ficam na lista até alguém (bot ou atendente) responder."""
    if not config.expediente_fechado_em:
        return []
    if config.expediente_aberto:
        aberto_em = config.expediente_aberto_em
        if not aberto_em or aberto_em < config.expediente_fechado_em or agora - aberto_em > _RETORNO_FECHADO_POR:
            return []
        fim = aberto_em
    else:
        fim = agora
    telefones = set(
        Mensagem.objects.filter(
            autor=Mensagem.Autor.CLIENTE, criado_em__gte=config.expediente_fechado_em, criado_em__lte=fim,
        ).values_list('telefone', flat=True)
    )
    # Quem está com pedido em andamento ou com atendente não recebeu o aviso e já aparece em outro alerta
    telefones -= set(
        SessaoChat.objects.filter(telefone__in=telefones)
        .exclude(estado_atual=SessaoChat.Estado.ATENDIMENTO_LLM).values_list('telefone', flat=True)
    )
    if config.expediente_aberto:
        telefones -= set(
            Mensagem.objects.filter(
                telefone__in=telefones, criado_em__gt=fim,
                autor__in=(Mensagem.Autor.BOT, Mensagem.Autor.ATENDENTE),
            ).values_list('telefone', flat=True)
        )
    clientes = _clientes_por_telefone(telefones)
    itens = [
        {'telefone': t, 'nome': clientes[t].nome_exibicao if t in clientes else t,
         'quando': clientes[t].ultima_mensagem_em if t in clientes else None}
        for t in telefones if not (t in clientes and clientes[t].bloqueado)
    ]
    itens.sort(key=lambda i: i['quando'] or agora, reverse=True)
    return itens


def _movimento_por_hora(desde: datetime) -> list[dict]:
    """Mensagens de clientes e pedidos por hora no dia comercial atual."""
    horas = [(settings.PAINEL_VIRADA_DIA_HORA + i) % 24 for i in range(24)]
    msgs = defaultdict(int)
    for criado_em in Mensagem.objects.filter(
        autor=Mensagem.Autor.CLIENTE, criado_em__gte=desde,
    ).values_list('criado_em', flat=True):
        msgs[timezone.localtime(criado_em).hour] += 1
    pedidos = defaultdict(int)
    for criado_em in Pedido.objects.filter(criado_em__gte=desde).exclude(
        status=Pedido.Status.CANCELADO,
    ).values_list('criado_em', flat=True):
        pedidos[timezone.localtime(criado_em).hour] += 1
    maximo = max(msgs.values(), default=0) or 1
    agora = timezone.localtime().hour
    return [
        {'hora': h, 'mensagens': msgs[h], 'pedidos': pedidos[h],
         'altura': round(msgs[h] / maximo * 100), 'atual': h == agora}
        for h in horas
    ]


def _contexto_inicio() -> dict:
    agora = timezone.now()
    config = Configuracao.obter()
    hoje = _dia_comercial(agora)
    inicio_hoje = _inicio_dia(hoje)
    faturados = Pedido.objects.filter(status__in=_FATURADOS)
    resumo_hoje = _somar(faturados.filter(criado_em__gte=inicio_hoje))
    # Mesmo dia da semana passada, até o mesmo horário: compara noite com noite
    semana_passada = _somar(faturados.filter(
        criado_em__gte=_inicio_dia(hoje - timedelta(days=7)), criado_em__lt=agora - timedelta(days=7),
    ))
    variacao = None
    if semana_passada['total']:
        variacao = float((resumo_hoje['total'] - semana_passada['total']) / semana_passada['total'] * 100)

    andamento = list(
        Pedido.objects.filter(status__in=_STATUS_ANDAMENTO).select_related('cliente')
        .prefetch_related('itens').order_by('criado_em')
    )
    for p in andamento:
        p.proximo = _PROXIMO_STATUS.get(p.status)
        p.proximo_rotulo = Pedido.Status(p.proximo).label if p.proximo else ''
    por_status = defaultdict(list)
    for p in andamento:
        por_status[p.status].append(p)
    funil = [
        {'status': s, 'rotulo': Pedido.Status(s).label, 'n': len(por_status[s]),
         'valor': sum((p.total for p in por_status[s]), Decimal('0'))}
        for s in _STATUS_ANDAMENTO
    ]
    pagamento_atrasado = [
        p for p in por_status[Pedido.Status.AGUARDANDO_PAGAMENTO]
        if agora - p.criado_em > _ALERTA_PAGAMENTO_APOS
    ]

    estoque = _estoque_baixo()
    esgotados = [e for e in estoque if e['estoque'] <= 0]
    baixos = [e for e in estoque if e['estoque'] > 0]
    atendente = _aguardando_atendente()
    chamaram = _chamaram_fechado(config, agora)
    nao_lidas = _conversas_qs('nao_lidas', '').exclude(estado=SessaoChat.Estado.ATENDIMENTO_HUMANO).count()

    alertas = (
        sum(1 for a in atendente if a['sem_resposta'])
        + len(por_status[Pedido.Status.COMPROVANTE_RECEBIDO])
        + len(por_status[Pedido.Status.PAGO]) + len(pagamento_atrasado)
        + len(esgotados) + len(chamaram)
    )
    return {
        'config': config,
        'saudacao': _saudacao(agora),
        'agora': agora,
        'hoje': resumo_hoje,
        'semana_passada': semana_passada,
        'variacao': variacao,
        'novos_clientes_hoje': Cliente.objects.filter(criado_em__gte=inicio_hoje).count(),
        'movimento': _movimento_por_hora(inicio_hoje),
        'virada': settings.PAINEL_VIRADA_DIA_HORA,
        'atendente': atendente,
        'atendente_respondidas': sum(1 for a in atendente if not a['sem_resposta']),
        'comprovantes': por_status[Pedido.Status.COMPROVANTE_RECEBIDO],
        'prontos': por_status[Pedido.Status.PAGO],
        'pagamento_atrasado': pagamento_atrasado,
        'alerta_pagamento_min': int(_ALERTA_PAGAMENTO_APOS.total_seconds() // 60),
        'esgotados': esgotados,
        'baixos': baixos,
        'chamaram_fechado': chamaram,
        'nao_lidas': nao_lidas,
        'alertas': alertas,
        'funil': funil,
        'andamento': andamento[:10],
        'conversas_recentes': _conversas_qs('', '')[:6],
        'estados': dict(SessaoChat.Estado.choices),
    }


@login_required
def inicio(request):
    contexto = _contexto_inicio()
    # Atualização automática (HTMX) só do miolo: o formulário do expediente fica intacto
    if request.headers.get('HX-Request'):
        return render(request, 'painel/parciais/inicio_painel.html', contexto)
    return _render(request, 'painel/inicio.html', {
        **contexto, 'secao': 'inicio', 'mensagem_padrao': MENSAGEM_FECHADO_PADRAO,
    })


@login_required
@require_POST
def expediente(request):
    """Abre/encerra o expediente, pausa o bot e edita a mensagem de loja fechada."""
    config = Configuracao.obter()
    acao = request.POST.get('acao')
    agora = timezone.now()
    if acao == 'fechar' and config.expediente_aberto:
        config.expediente_aberto, config.expediente_fechado_em = False, agora
        config.save(update_fields=['expediente_aberto', 'expediente_fechado_em'])
        auditoria.registrar(
            request, Tipo.CONFIGURACAO, 'expediente_fechar', 'Encerrou o expediente (bot pausado)',
            alvo=config, mudancas={'expediente_aberto': [True, False]},
        )
        messages.success(request, 'Atividades encerradas. Quem chamar agora recebe a mensagem de loja fechada.')
    elif acao == 'abrir' and not config.expediente_aberto:
        config.expediente_aberto, config.expediente_aberto_em = True, agora
        config.save(update_fields=['expediente_aberto', 'expediente_aberto_em'])
        auditoria.registrar(
            request, Tipo.CONFIGURACAO, 'expediente_abrir', 'Abriu o expediente (bot atendendo)',
            alvo=config, mudancas={'expediente_aberto': [False, True]},
        )
        messages.success(request, 'Loja aberta! O bot voltou a atender.')
    elif acao == 'pausar' and not config.bot_pausado:
        config.bot_pausado, config.bot_pausado_em = True, agora
        config.save(update_fields=['bot_pausado', 'bot_pausado_em'])
        auditoria.registrar(
            request, Tipo.CONFIGURACAO, 'bot_pausar', 'Pausou o bot (sem nenhuma resposta automática)',
            alvo=config, mudancas={'bot_pausado': [False, True]},
        )
        messages.success(request, 'Bot pausado. Ninguém recebe resposta automática; as mensagens continuam chegando aqui.')
    elif acao == 'retomar' and config.bot_pausado:
        config.bot_pausado = False
        config.save(update_fields=['bot_pausado'])
        auditoria.registrar(
            request, Tipo.CONFIGURACAO, 'bot_retomar', 'Retomou o bot',
            alvo=config, mudancas={'bot_pausado': [True, False]},
        )
        messages.success(request, 'Bot retomado.' + ('' if config.expediente_aberto else ' A loja segue fechada: quem chamar recebe a mensagem de loja fechada.'))
    elif acao == 'mensagem':
        antes = {'horario_atendimento': config.horario_atendimento, 'mensagem_fechado': config.mensagem_fechado}
        config.horario_atendimento = request.POST.get('horario_atendimento', '').strip()[:120]
        config.mensagem_fechado = request.POST.get('mensagem_fechado', '').strip()
        config.save(update_fields=['horario_atendimento', 'mensagem_fechado'])
        mudancas = {k: [v, getattr(config, k)] for k, v in antes.items() if v != getattr(config, k)}
        if mudancas:
            auditoria.registrar(
                request, Tipo.CONFIGURACAO, 'expediente_mensagem', 'Editou a mensagem de loja fechada',
                alvo=config, mudancas=mudancas,
            )
        messages.success(request, 'Mensagem de loja fechada salva.')
    return redirect('painel:inicio')


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

    vendas_periodo = faturados.filter(criado_em__gte=inicio_serie)
    por_hora = _vendas_por_hora(vendas_periodo)
    novos_clientes = Cliente.objects.filter(criado_em__gte=inicio_serie).count()

    return _render(request, 'painel/dashboard.html', {
        'secao': 'dashboard',
        'dias': dias,
        'hoje': resumo_hoje,
        'periodo': resumo_periodo,
        'variacao': variacao,
        'serie': serie,
        'top_produtos': top_produtos,
        'por_hora': por_hora,
        'pico_hora': max(por_hora, key=lambda h: h['pedidos']) if any(h['pedidos'] for h in por_hora) else None,
        'por_vendedor': _vendas_por_vendedor(vendas_periodo, resumo_periodo['total']),
        'por_dia_semana': _vendas_por_dia_semana(
            vendas_periodo, [hoje - timedelta(days=i) for i in range(dias)],
        ),
        'melhores_clientes': _melhores_clientes(vendas_periodo),
        'novos_clientes': novos_clientes,
        'virada': settings.PAINEL_VIRADA_DIA_HORA,
    })


def _vendas_por_hora(vendas) -> list[dict]:
    """Pedidos pagos e faturamento por hora do dia, na ordem do dia comercial."""
    por_hora = defaultdict(lambda: {'pedidos': 0, 'total': Decimal('0')})
    for criado_em, total in vendas.values_list('criado_em', 'total'):
        h = por_hora[timezone.localtime(criado_em).hour]
        h['pedidos'] += 1
        h['total'] += total
    horas = [(settings.PAINEL_VIRADA_DIA_HORA + i) % 24 for i in range(24)]
    return [
        {'hora': h, 'rotulo': f'{h}h', 'pedidos': por_hora[h]['pedidos'], 'total': float(por_hora[h]['total'])}
        for h in horas
    ]


def _vendas_por_vendedor(vendas, total_periodo: Decimal) -> list[dict]:
    """Quem vendeu: o usuário que lançou a venda manual ou aprovou o pagamento do pedido
    do WhatsApp. Sobra para o bot só pedido faturado sem ninguém registrado."""
    linhas = (
        vendas.order_by().values('origem', 'vendedor_id', 'vendedor__username', 'vendedor__first_name', 'vendedor__last_name')
        .annotate(pedidos=Count('id'), total=Sum('total'))
    )
    # Um usuário pode ter vendas manuais e do WhatsApp: soma tudo numa linha só
    por_vendedor = {}
    for v in linhas:
        if v['vendedor_id']:
            chave = v['vendedor_id']
            nome = f"{v['vendedor__first_name']} {v['vendedor__last_name']}".strip() or v['vendedor__username']
        elif v['origem'] != Pedido.Origem.MANUAL:
            chave, nome = 'bot', '🤖 Bot (sem aprovação registrada)'
        else:
            chave, nome = 'manual', 'Venda manual (sem vendedor)'
        linha = por_vendedor.setdefault(chave, {'nome': nome, 'pedidos': 0, 'total': Decimal('0')})
        linha['pedidos'] += v['pedidos']
        linha['total'] += v['total']
    resultado = [
        {**r, 'ticket': r['total'] / r['pedidos'],
         'parcela': float(r['total'] / total_periodo * 100) if total_periodo else 0}
        for r in por_vendedor.values()
    ]
    return sorted(resultado, key=lambda r: r['total'], reverse=True)


_DIAS_SEMANA = ('Segunda', 'Terça', 'Quarta', 'Quinta', 'Sexta', 'Sábado', 'Domingo')


def _vendas_por_dia_semana(vendas, datas: list) -> list[dict]:
    """Faturamento por dia da semana (pelo dia comercial: 01h de sábado conta na sexta)
    e a média por noite, contando todas as noites do período, inclusive as sem venda."""
    total = defaultdict(Decimal)
    pedidos = defaultdict(int)
    for criado_em, valor in vendas.values_list('criado_em', 'total'):
        dia = _dia_comercial(criado_em).weekday()
        total[dia] += valor
        pedidos[dia] += 1
    noites = defaultdict(int)
    for data in datas:
        noites[data.weekday()] += 1
    maximo = max(total.values(), default=Decimal('0')) or Decimal('1')
    return [
        {'dia': nome, 'total': total[i], 'pedidos': pedidos[i],
         'media': total[i] / noites[i] if noites[i] else Decimal('0'),
         'largura': round(total[i] / maximo * 100)}
        for i, nome in enumerate(_DIAS_SEMANA)
    ]


def _melhores_clientes(vendas, limite: int = 6) -> list[dict]:
    linhas = (
        vendas.filter(cliente__isnull=False).order_by().values('cliente')
        .annotate(pedidos=Count('id'), total=Sum('total')).order_by('-total')[:limite]
    )
    clientes = Cliente.objects.in_bulk([l['cliente'] for l in linhas])
    return [{**l, 'cliente': clientes[l['cliente']]} for l in linhas if l['cliente'] in clientes]


def _estoque_baixo() -> list[dict]:
    """Produtos e opções ativos no estoque mínimo ou abaixo, os mais críticos primeiro."""
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
    return estoque_baixo


# ── Conversas ──


def _conversas_qs(filtro: str, busca: str, loja: str = ''):
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
        .select_related('loja_atual')
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
    if loja.isdigit():
        qs = qs.filter(loja_atual_id=loja)
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
    loja = request.GET.get('loja', '')
    contexto = {
        'secao': 'conversas',
        'filtro': filtro,
        'busca': busca,
        'loja': loja,
        'conversas': _conversas_qs(filtro, busca, loja)[:100],
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
    pedidos_abertos = list(cliente.pedidos.filter(status__in=(*_ABERTOS, Pedido.Status.PAGO, Pedido.Status.EM_ENTREGA)))
    for p in pedidos_abertos:
        p.proximo = _PROXIMO_STATUS.get(p.status)
        p.proximo_rotulo = Pedido.Status(p.proximo).label if p.proximo else ''
    respostas = list(RespostaRapida.objects.filter(ativo=True))
    return _render(request, 'painel/conversa.html', {
        'secao': 'conversas',
        'cliente': cliente,
        'sessao': sessao,
        'mensagens': mensagens,
        'ultimo_id': mensagens[-1].pk if mensagens else 0,
        'pedidos_abertos': pedidos_abertos,
        'em_atendimento_humano': bool(sessao and sessao.estado_atual == SessaoChat.Estado.ATENDIMENTO_HUMANO),
        'respostas_botoes': [r for r in respostas if r.mostrar_botao],
        'comandos_chat': _comandos_chat(cliente, pedidos_abertos, respostas),
        'form_resposta': RespostaRapidaForm(),
    })


def _resumo_pedido(pedido: Pedido) -> str:
    linhas = ['*Seu pedido*']  # vai para o cliente: sem o número interno
    linhas += [f'{i.quantidade}x {i.nome}' for i in pedido.itens.all()]
    if pedido.frete:
        linhas.append(f'Frete: {formatar_preco(float(pedido.frete))}')
    linhas.append(f'*Total: {formatar_preco(float(pedido.total))}*')
    return '\n'.join(linhas)


def _comandos_chat(cliente: Cliente, pedidos_abertos: list, respostas: list) -> dict:
    """Dados dos comandos "/" da caixa de resposta: respostas rápidas (com as
    variáveis já resolvidas para este cliente), comandos fixos e modelos do
    cardápio com os sabores em estoque."""
    pedido = pedidos_abertos[0] if pedidos_abertos else None
    nome_completo = cliente.nome or cliente.nome_whatsapp
    nome = nome_completo.split()[0] if nome_completo.strip() else ''
    variaveis = {
        'nome': nome,
        'pix': settings.CHAVE_PIX,
        'endereco': pedido.endereco if pedido else '',
        'pedido': f'#{pedido.pk}' if pedido else '',
        'total': formatar_preco(float(pedido.total)) if pedido else '',
        'horario': Configuracao.obter().horario_atendimento,
    }
    fixos = [
        {'atalho': 'nome', 'titulo': 'Nome do cliente', 'texto': nome},
        {'atalho': 'endereco', 'titulo': 'Endereço de entrega', 'texto': variaveis['endereco']},
        {'atalho': 'pedido', 'titulo': f'Resumo do pedido {variaveis["pedido"]}'.strip(),
         'texto': _resumo_pedido(pedido) if pedido else ''},
    ]
    produtos = [
        {
            # "Elf Bar 45k (45.000 puffs)" -> /elfbar45k
            'atalho': slugify(re.sub(r'\([^)]*\)', '', item['nome'])).replace('-', ''),
            'titulo': item['nome'],
            'detalhe': formatar_preco(item['preco']) + (
                f' · {n} sabor{"es" if n > 1 else ""}' if (n := len(item['variantes'])) else ''
            ),
            'texto': gerar_sabores_texto(item),
        }
        for categoria in catalogo.atualizado()['categorias'] for item in categoria['itens']
    ]
    return {
        'variaveis': variaveis,
        'respostas': [{'atalho': r.atalho, 'titulo': r.titulo, 'texto': r.texto} for r in respostas],
        'fixos': [c for c in fixos if c['texto']],
        'produtos': produtos,
    }


def _sessao(telefone: str) -> SessaoChat:
    sessao, _ = SessaoChat.objects.get_or_create(telefone=telefone)
    return sessao


def _nome_conversa(telefone: str) -> str:
    cliente = Cliente.objects.filter(telefone=telefone).first()
    return cliente.nome_exibicao if cliente else telefone


@login_required
@require_POST
def conversa_responder(request, telefone: str):
    texto = request.POST.get('texto', '').strip()
    if texto:
        sessao = _sessao(telefone)
        # Atendente respondeu: o bot para de responder até devolverem a conversa
        assumiu = sessao.estado_atual != SessaoChat.Estado.ATENDIMENTO_HUMANO
        if assumiu:
            sessao.estado_atual = SessaoChat.Estado.ATENDIMENTO_HUMANO
            sessao.ultima_interacao = timezone.now()
            sessao.save(update_fields=['estado_atual', 'ultima_interacao'])
        ok = async_to_sync(enviar_whatsapp)(telefone, texto, 'atendente')
        previa = ' '.join(texto.split())
        previa = previa if len(previa) <= 80 else previa[:79] + '…'
        auditoria.registrar(
            request, Tipo.CONVERSA, 'conversa_responder',
            f'{"Respondeu" if ok else "Falhou ao responder"} {_nome_conversa(telefone)}: "{previa}"'
            + (' (assumiu a conversa)' if assumiu else ''),
            alvo=('conversa', telefone),
        )
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
    auditoria.registrar(
        request, Tipo.CONVERSA, 'conversa_assumir',
        f'Assumiu a conversa com {_nome_conversa(telefone)}', alvo=('conversa', telefone),
    )
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
    auditoria.registrar(
        request, Tipo.CONVERSA, 'conversa_devolver',
        f'Devolveu ao bot a conversa com {_nome_conversa(telefone)}'
        + (' (cliente avisado)' if request.POST.get('avisar') else ''),
        alvo=('conversa', telefone),
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
    # Ordenação clicando no cabeçalho: coluna -> (campo, direção padrão)
    colunas = {
        'nome': ('nome', 'asc'), 'pedidos': ('num_pedidos', 'desc'), 'gasto': ('total_gasto', 'desc'),
        'ultimo_pedido': ('ultimo_pedido', 'desc'), 'recentes': ('ultima_mensagem_em', 'desc'),
    }
    if ordem not in colunas:
        ordem = 'recentes'
    campo, padrao = colunas[ordem]
    direcao = request.GET.get('dir') if request.GET.get('dir') in ('asc', 'desc') else padrao
    # Vazios (sem pedido/mensagem) sempre no fim, nos dois sentidos
    expr = F(campo).asc(nulls_last=True) if direcao == 'asc' else F(campo).desc(nulls_last=True)
    qs = qs.order_by(expr, *(['nome_whatsapp'] if ordem == 'nome' else []), 'pk')
    pagina = Paginator(qs, 50).get_page(request.GET.get('pagina'))
    return _render(request, 'painel/clientes.html', {
        'secao': 'clientes', 'pagina': pagina, 'busca': busca, 'ordem': ordem, 'direcao': direcao,
        'cabecalho': [
            {'chave': chave, 'rotulo': rotulo, 'num': num, 'ativa': chave == ordem,
             'dir': ('asc' if direcao == 'desc' else 'desc') if chave == ordem else colunas[chave][1]}
            for chave, rotulo, num in (
                ('nome', 'Cliente', False), ('pedidos', 'Pedidos', True), ('gasto', 'Total gasto', True),
                ('ultimo_pedido', 'Último pedido', False), ('recentes', 'Última mensagem', False),
            )
        ],
        'total_clientes': Cliente.objects.count(),
    })


@login_required
def cliente(request, pk: int):
    obj = get_object_or_404(Cliente, pk=pk)
    if request.method == 'POST':
        form = ClienteForm(request.POST, instance=obj)
        if form.is_valid():
            mudancas = auditoria.mudancas_form(form)
            form.save()
            if mudancas:
                if 'bloqueado' in form.changed_data:
                    acao = 'Bloqueou' if obj.bloqueado else 'Desbloqueou'
                else:
                    acao = 'Editou'
                auditoria.registrar(
                    request, Tipo.CLIENTE, 'cliente_editar', f'{acao} o cliente {obj.nome_exibicao}',
                    alvo=obj, mudancas=mudancas,
                )
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
# (sem o número do pedido: ele é só para controle interno da loja)
_AVISOS_STATUS = {
    Pedido.Status.PAGO: '✅ Pagamento confirmado! Seu pedido já está sendo preparado.',
    Pedido.Status.EM_ENTREGA: '🛵 Seu pedido saiu para entrega! Já já chega aí.',
    Pedido.Status.ENTREGUE: '📦 Pedido entregue. Obrigado pela preferência! 💜',
    Pedido.Status.CANCELADO: '❌ Seu pedido foi cancelado. Qualquer dúvida, é só chamar.',
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
    loja = request.GET.get('loja', '')
    qs = Pedido.objects.select_related('cliente', 'loja').prefetch_related('itens')
    if loja.isdigit():
        qs = qs.filter(loja_id=loja)
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
        'secao': 'pedidos', 'pagina': pagina, 'status': status, 'busca': busca, 'loja': loja,
        'status_opcoes': [(v, r, contagem.get(v, 0)) for v, r in Pedido.Status.choices],
    })


@login_required
def mensagem_midia(request, pk: int):
    """Foto/PDF enviado pelo cliente na conversa, servido só a quem está logado."""
    obj = get_object_or_404(Mensagem, pk=pk)
    if not obj.midia:
        raise Http404
    try:
        arquivo = obj.midia.open('rb')
    except FileNotFoundError:
        raise Http404
    resposta = FileResponse(arquivo, content_type=mimetypes.guess_type(obj.midia.name)[0] or 'application/octet-stream')
    resposta['Content-Disposition'] = f'inline; filename="{os.path.basename(obj.midia.name)}"'
    resposta['Cache-Control'] = 'private, max-age=3600'
    return resposta


@login_required
def pedido(request, pk: int):
    obj = get_object_or_404(Pedido.objects.select_related('cliente').prefetch_related('itens'), pk=pk)
    obj.proximo = _PROXIMO_STATUS.get(obj.status)
    obj.proximo_rotulo = Pedido.Status(obj.proximo).label if obj.proximo else ''
    return _render(request, 'painel/pedido.html', {
        'secao': 'pedidos', 'pedido': obj, 'status_choices': Pedido.Status.choices,
    })


@login_required
def pedido_comprovante(request, pk: int):
    """Imagem/PDF do comprovante, servido só a quem está logado no painel."""
    obj = get_object_or_404(Pedido, pk=pk)
    if not obj.comprovante:
        raise Http404
    try:
        arquivo = obj.comprovante.open('rb')
    except FileNotFoundError:
        raise Http404
    resposta = FileResponse(arquivo, content_type=mimetypes.guess_type(obj.comprovante.name)[0] or 'application/octet-stream')
    resposta['Content-Disposition'] = f'inline; filename="{os.path.basename(obj.comprovante.name)}"'
    resposta['Cache-Control'] = 'private, max-age=3600'
    return resposta


@login_required
@require_POST
def pedido_status(request, pk: int):
    obj = get_object_or_404(Pedido.objects.select_related('cliente'), pk=pk)
    novo = request.POST.get('status', '')
    if novo not in Pedido.Status.values or novo == obj.status:
        return redirect(request.POST.get('voltar') or 'painel:pedidos')

    anterior = obj.get_status_display()
    servicos.mudar_status(obj, novo)
    # Quem aprova o pagamento de um pedido do bot fica como vendedor dele
    if novo in _FATURADOS and obj.status not in _FATURADOS:
        Pedido.objects.filter(pk=obj.pk, vendedor__isnull=True).update(vendedor=request.user)
    rotulo = Pedido.Status(novo).label
    aviso = ''
    if request.POST.get('avisar') and novo in _AVISOS_STATUS and obj.cliente:
        ok = async_to_sync(enviar_whatsapp)(obj.cliente.telefone, _AVISOS_STATUS[novo])
        aviso = ' — cliente avisado' if ok else ' — falha ao avisar no WhatsApp'
        messages.success(request, f'Pedido #{obj.pk}: {rotulo}{aviso}.')
    else:
        messages.success(request, f'Pedido #{obj.pk}: {rotulo}.')
    auditoria.registrar(
        request, Tipo.PEDIDO, 'pedido_status', f'Pedido #{obj.pk}: {rotulo}{aviso}',
        alvo=obj, mudancas={'Status': [anterior, rotulo]},
    )

    # Pedido encerrado: a conversa volta para o bot
    if novo in (Pedido.Status.ENTREGUE, Pedido.Status.CANCELADO) and obj.cliente:
        sessao = SessaoChat.objects.filter(telefone=obj.cliente.telefone).first()
        if sessao and sessao.carrinho_temporario.get('pedido_id') == obj.pk:
            sessao.resetar_sessao()
            sessao.save()
    return redirect(request.POST.get('voltar') or 'painel:pedidos')


# Status possíveis ao lançar uma venda manual (o primeiro é o padrão)
_STATUS_VENDA_MANUAL = (Pedido.Status.ENTREGUE, Pedido.Status.PAGO, Pedido.Status.AGUARDANDO_PAGAMENTO)


def _opcoes_venda() -> list[dict]:
    """Itens vendáveis agrupados por categoria. Produto com opções é vendido
    pela opção, que é onde fica o estoque."""
    grupos = []
    for cat in Categoria.objects.prefetch_related('produtos__variantes'):
        itens = []
        for p in cat.produtos.all():
            variantes = list(p.variantes.all())
            for v in variantes:
                itens.append({
                    'id': f'v{v.pk}', 'nome': f'{p.nome} - {v.nome}', 'preco': str(p.preco),
                    'estoque': v.estoque, 'oculto': not (p.ativo and v.ativo),
                })
            if not variantes:
                itens.append({
                    'id': f'p{p.pk}', 'nome': p.nome, 'preco': str(p.preco),
                    'estoque': p.estoque, 'oculto': not p.ativo,
                })
        if itens:
            grupos.append({'titulo': cat.titulo, 'itens': itens})
    return grupos


def _ler_itens_venda(post) -> tuple[list[dict], list[str]]:
    itens, erros = [], []
    produtos = {p.pk: p for p in Produto.objects.all()}
    variantes = {v.pk: v for v in Variante.objects.all()}
    for ref, qtd, preco in zip(post.getlist('item'), post.getlist('qtd'), post.getlist('preco')):
        if not ref:
            continue
        if ref.startswith('v') and ref[1:].isdigit() and int(ref[1:]) in variantes:
            variante = variantes[int(ref[1:])]
            produto = produtos[variante.produto_id]
        elif ref.startswith('p') and ref[1:].isdigit() and int(ref[1:]) in produtos:
            variante, produto = None, produtos[int(ref[1:])]
        else:
            erros.append('Item não encontrado no catálogo.')
            continue
        nome = f'{produto.nome} - {variante.nome}' if variante else produto.nome
        try:
            quantidade = int(qtd)
            if quantidade < 1:
                raise ValueError
        except ValueError:
            erros.append(f'Quantidade inválida para {nome}.')
            continue
        try:
            preco_unitario = Decimal(preco.replace(',', '.')) if preco.strip() else produto.preco
            if not preco_unitario.is_finite() or preco_unitario < 0:
                raise ArithmeticError
        except ArithmeticError:
            erros.append(f'Preço inválido para {nome}.')
            continue
        itens.append({
            'produto': produto, 'variante': variante, 'quantidade': quantidade,
            'preco_unitario': preco_unitario.quantize(Decimal('0.01')),
        })
    if not itens and not erros:
        erros.append('Adicione pelo menos um item.')
    return itens, erros


def _normalizar_telefone(texto: str) -> str | None:
    """'(11) 98888-7777' -> '5511988887777'. Vazio -> ''. Inválido -> None."""
    digitos = ''.join(c for c in texto if c.isdigit())
    if not digitos:
        return ''
    if len(digitos) in (10, 11):
        digitos = '55' + digitos
    return digitos if len(digitos) in (12, 13) and digitos.startswith('55') else None


@login_required
def venda_manual(request):
    """Lança uma venda feita fora do bot para o estoque e os relatórios baterem."""
    agora_local = timezone.localtime().replace(second=0, microsecond=0)
    dados = {
        'telefone': '', 'nome': '', 'frete': '', 'observacoes': '',
        'status': _STATUS_VENDA_MANUAL[0], 'quando': agora_local.strftime('%Y-%m-%dT%H:%M'),
        'itens': [],
    }
    if request.method == 'POST':
        post = request.POST
        dados.update({k: post.get(k, '').strip() for k in ('telefone', 'nome', 'frete', 'observacoes', 'status', 'quando')})
        dados['itens'] = [
            {'id': i, 'qtd': q, 'preco': p}
            for i, q, p in zip(post.getlist('item'), post.getlist('qtd'), post.getlist('preco')) if i
        ]
        itens, erros = _ler_itens_venda(post)

        telefone = _normalizar_telefone(dados['telefone'])
        if telefone is None:
            erros.append('Telefone inválido. Use DDD + número, ex.: (11) 98888-7777.')
        if dados['status'] not in _STATUS_VENDA_MANUAL:
            erros.append('Status inválido.')
        try:
            frete = Decimal(dados['frete'].replace(',', '.')) if dados['frete'] else Decimal('0')
            if not frete.is_finite() or frete < 0:
                raise ArithmeticError
        except ArithmeticError:
            erros.append('Frete inválido.')
        try:
            quando = timezone.make_aware(datetime.fromisoformat(dados['quando']))
            if quando > timezone.now() + timedelta(minutes=5):
                erros.append('A data da venda não pode estar no futuro.')
        except ValueError:
            erros.append('Data/hora inválida.')

        if not erros:
            pedido = servicos.registrar_venda_manual(
                itens, status=dados['status'], criado_em=quando, telefone=telefone,
                nome=dados['nome'], frete=frete.quantize(Decimal('0.01')),
                observacoes=dados['observacoes'], vendedor=request.user,
            )
            n_itens = sum(i['quantidade'] for i in itens)
            auditoria.registrar(
                request, Tipo.PEDIDO, 'venda_manual',
                f'Lançou a venda manual #{pedido.pk}: {n_itens} {"item" if n_itens == 1 else "itens"}, '
                f'total R$ {auditoria.texto(pedido.total)} ({pedido.get_status_display()})',
                alvo=pedido,
            )
            messages.success(request, f'Venda manual #{pedido.pk} lançada e estoque atualizado.')
            negativos = [
                f'{i.nome} ({e})' for i in pedido.itens.select_related('produto', 'variante')
                if (e := (i.variante.estoque if i.variante else i.produto.estoque)) is not None and e < 0
            ]
            if negativos:
                messages.warning(
                    request, 'Estoque ficou negativo em: ' + ', '.join(negativos)
                    + '. Confira a contagem e corrija na tela de Estoque.',
                )
            return redirect('painel:pedido', pk=pedido.pk)
        for erro in dict.fromkeys(erros):
            messages.error(request, erro)

    return _render(request, 'painel/venda_manual.html', {
        'secao': 'pedidos',
        'dados': dados,
        'opcoes': _opcoes_venda(),
        'status_opcoes': [(s.value, s.label) for s in _STATUS_VENDA_MANUAL],
    })


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
    antes = {'estoque': obj.estoque, 'preco': getattr(obj, 'preco', None), 'ativo': obj.ativo}
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
        rotulos = {'estoque': 'Estoque', 'preco': 'Preço', 'ativo': 'Ativo'}
        mudancas = {
            rotulos[c]: [auditoria.texto(antes[c]), auditoria.texto(getattr(obj, c))]
            for c in dict.fromkeys(campos) if antes[c] != getattr(obj, c)
        }
        if mudancas:
            # Cliques seguidos (+1 +1 +1) viram um registro só
            auditoria.registrar(
                request, Tipo.ESTOQUE, 'estoque_ajustar', f'Ajustou {obj}',
                alvo=obj if tipo == 'produto' else obj.produto, mudancas=mudancas,
                agrupar_minutos=3,
            )

    produto = obj if tipo == 'produto' else obj.produto
    produto = Produto.objects.prefetch_related('variantes').get(pk=produto.pk)
    return render(request, 'painel/parciais/estoque_produto.html', {'p': produto})


def _mudancas_variantes(formset) -> dict:
    """Opções incluídas, removidas e editadas num formset já validado."""
    mudancas = {}
    for f in formset.forms:
        if not f.has_changed():
            continue
        nome = f.cleaned_data.get('nome') or f.initial.get('nome') or '?'
        if f in formset.deleted_forms:
            if f.instance.pk:
                mudancas[f'Opção {f.initial.get("nome", nome)}'] = ['existia', 'removida']
        elif not f.instance.pk:
            mudancas[f'Opção {nome}'] = ['—', 'incluída']
        else:
            mudancas.update(auditoria.mudancas_form(f, prefixo=f'Opção {f.initial.get("nome", nome)} · '))
    return mudancas


@login_required
def produto_editar(request, pk: int | None = None):
    obj = get_object_or_404(Produto, pk=pk) if pk else None
    if request.method == 'POST':
        if 'excluir' in request.POST and obj:
            nome = obj.nome
            obj.delete()
            auditoria.registrar(request, Tipo.ESTOQUE, 'produto_excluir', f'Excluiu o produto "{nome}"')
            messages.success(request, f'Produto "{nome}" excluído.')
            return redirect('painel:estoque')
        form = ProdutoForm(request.POST, instance=obj)
        formset = VarianteFormSet(request.POST, instance=obj or Produto())
        if form.is_valid() and formset.is_valid():
            mudancas = auditoria.mudancas_form(form) if obj else {}
            mudancas.update(_mudancas_variantes(formset))
            produto = form.save()
            formset.instance = produto
            formset.save()
            if not obj:
                auditoria.registrar(
                    request, Tipo.ESTOQUE, 'produto_criar', f'Cadastrou o produto "{produto.nome}"',
                    alvo=produto, mudancas=mudancas,
                )
            elif mudancas:
                auditoria.registrar(
                    request, Tipo.ESTOQUE, 'produto_editar', f'Editou o produto "{produto.nome}"',
                    alvo=produto, mudancas=mudancas,
                )
            messages.success(request, f'Produto "{produto.nome}" salvo.')
            return redirect('painel:estoque')
    else:
        form = ProdutoForm(instance=obj)
        formset = VarianteFormSet(instance=obj or Produto())
    return _render(request, 'painel/produto_form.html', {
        'secao': 'estoque', 'form': form, 'formset': formset, 'produto': obj,
        'sem_categoria': not Categoria.objects.exists(),
    })


# O WhatsApp aceita documentos de até 100 MB; acima de ~20 MB o cliente já
# sofre para baixar no 4G.
_CARDAPIO_PDF_MAX_MB = 30


def _salvar_cardapio_pdf(request, config: Configuracao) -> None:
    """Troca ou remove o PDF do cardápio enviado nas boas-vindas."""
    if request.POST.get('cardapio_pdf') == 'remover':
        config.cardapio_pdf.delete(save=True)
        auditoria.registrar(
            request, Tipo.CONFIGURACAO, 'cardapio_pdf', 'Removeu o PDF do cardápio', alvo=config,
        )
        messages.success(request, 'PDF do cardápio removido. O bot manda só o cardápio em texto.')
        return

    arquivo = request.FILES.get('cardapio_pdf')
    if not arquivo:
        messages.error(request, 'Escolha um arquivo PDF.')
        return
    if not arquivo.name.lower().endswith('.pdf'):
        messages.error(request, 'O arquivo precisa ser um PDF.')
        return
    if arquivo.size > _CARDAPIO_PDF_MAX_MB * 1024 * 1024:
        messages.error(request, f'O PDF passa de {_CARDAPIO_PDF_MAX_MB} MB. Comprima o arquivo e tente de novo.')
        return

    if config.cardapio_pdf:
        config.cardapio_pdf.delete(save=False)
    config.cardapio_pdf.save('cardapio.pdf', arquivo, save=True)
    auditoria.registrar(
        request, Tipo.CONFIGURACAO, 'cardapio_pdf', f'Trocou o PDF do cardápio ({arquivo.name})',
        alvo=config,
    )
    messages.success(request, 'PDF do cardápio atualizado. Ele vai junto das boas-vindas para novos clientes.')


@login_required
def cardapio_pdf(request):
    config = Configuracao.obter()
    if not config.cardapio_pdf:
        raise Http404
    return FileResponse(config.cardapio_pdf.open('rb'), content_type='application/pdf', filename='cardapio.pdf')


@login_required
def configuracoes(request):
    config = Configuracao.obter()
    if request.method == 'POST':
        if 'testar' in request.POST:
            ok = async_to_sync(notificar_equipe)(
                '✅ *Teste do Robopod*\nOs avisos da loja vão chegar neste grupo.',
            )
            auditoria.registrar(
                request, Tipo.CONFIGURACAO, 'avisos_teste',
                'Enviou mensagem de teste ao grupo de avisos' if ok else 'Falhou ao enviar o teste ao grupo de avisos',
                alvo=config,
            )
            if ok:
                messages.success(request, 'Mensagem de teste enviada ao grupo.')
            else:
                messages.error(request, 'Não foi possível enviar: confira o grupo escolhido e a conexão da Evolution.')
            return redirect('painel:configuracoes')

        if 'cardapio_pdf' in request.POST or 'cardapio_pdf' in request.FILES:
            _salvar_cardapio_pdf(request, config)
            return redirect('painel:configuracoes')

        campos_aviso = {
            'grupo_avisos_nome': 'Grupo de avisos',
            'avisos_ativos': 'Avisos ativos',
            'avisar_mensagens_atendimento': 'Repassar mensagens em atendimento',
        }
        antes = {c: getattr(config, c) for c in campos_aviso}
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
        mudancas = {
            rotulo: [auditoria.texto(antes[c]), auditoria.texto(getattr(config, c))]
            for c, rotulo in campos_aviso.items() if antes[c] != getattr(config, c)
        }
        if mudancas:
            auditoria.registrar(
                request, Tipo.CONFIGURACAO, 'configuracoes', 'Alterou os avisos da equipe',
                alvo=config, mudancas=mudancas,
            )
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
        'push_chave': settings.VAPID_PUBLIC_KEY if push.ativo() else '',
        'push_eventos': [{'chave': k, 'nome': nome, 'padrao': padrao} for k, (nome, padrao) in push.EVENTOS.items()],
    })


# ── Lojas: os números de WhatsApp da operação (só administradores) ──

_INSTANCIA_VALIDA = re.compile(r'^[A-Za-z0-9_-]{1,60}$')
_ESTADOS_CONEXAO = {
    'open': ('Conectado', 'pago'), 'connecting': ('Aguardando QR Code', 'aguardando_pagamento'),
    'close': ('Desconectado', 'cancelado'), 'inexistente': ('Não existe na Evolution', 'cancelado'),
    '': ('Evolution não respondeu', ''),
}


@login_required
def lojas(request):
    if not request.user.is_superuser:
        raise PermissionDenied
    if request.method == 'POST':
        acao = request.POST.get('acao', '')
        if acao == 'criar':
            return _criar_loja(request)
        loja = get_object_or_404(Loja, pk=request.POST.get('loja'))
        if acao == 'principal':
            Loja.objects.update(principal=False)
            Loja.objects.filter(pk=loja.pk).update(principal=True, ativa=True)
            auditoria.registrar(request, Tipo.CONFIGURACAO, 'loja_principal',
                                f'Definiu {loja.nome} como número principal (avisos ao grupo)', alvo=loja)
            messages.success(request, f'{loja.nome} agora manda os avisos ao grupo. Ela precisa participar do grupo.')
        elif acao == 'salvar':
            antes = {'Nome': loja.nome, 'Ativa': loja.ativa}
            loja.nome = request.POST.get('nome', '').strip()[:60] or loja.nome
            loja.ativa = bool(request.POST.get('ativa')) or loja.principal
            loja.save(update_fields=['nome', 'ativa'])
            depois = {'Nome': loja.nome, 'Ativa': loja.ativa}
            mudancas = {
                c: [auditoria.texto(antes[c]), auditoria.texto(depois[c])] for c in antes if antes[c] != depois[c]
            }
            if mudancas:
                auditoria.registrar(request, Tipo.CONFIGURACAO, 'loja_editar', f'Alterou a loja {loja.nome}',
                                    alvo=loja, mudancas=mudancas)
            messages.success(request, 'Loja salva.')
        return redirect('painel:lojas')

    lista = list(Loja.objects.annotate(num_clientes=Count('clientes')).order_by('-principal', 'pk'))
    for loja in lista:
        estado = async_to_sync(estado_conexao)(loja.instancia)
        loja.estado, loja.estado_classe = _ESTADOS_CONEXAO.get(estado, (estado, ''))
    qr = None
    if request.GET.get('qr'):
        qr_loja = Loja.objects.filter(pk=request.GET['qr']).first()
        if qr_loja is not None:
            qr = {'loja': qr_loja, 'imagem': async_to_sync(qr_code)(qr_loja.instancia)}
    return _render(request, 'painel/lojas.html', {'secao': 'configuracoes', 'lojas': lista, 'qr': qr})


def _criar_loja(request):
    nome = request.POST.get('nome', '').strip()[:60]
    instancia = request.POST.get('instancia', '').strip()
    if not nome or not _INSTANCIA_VALIDA.match(instancia):
        messages.error(request, 'Preencha o nome e uma instância só com letras, números, - ou _ (ex.: loja2).')
        return redirect('painel:lojas')
    if Loja.objects.filter(instancia=instancia).exists():
        messages.error(request, f'Já existe uma loja com a instância "{instancia}".')
        return redirect('painel:lojas')
    try:
        async_to_sync(criar_instancia)(instancia)
    except Exception as e:
        logger.warning('Falha ao criar a instância %s na Evolution: %s', instancia, e)
        messages.error(request, 'A Evolution não criou a instância. Confira se ela está no ar e tente de novo.')
        return redirect('painel:lojas')
    loja = Loja.objects.create(nome=nome, instancia=instancia, principal=not Loja.objects.exists())
    auditoria.registrar(request, Tipo.CONFIGURACAO, 'loja_criar',
                        f'Cadastrou a loja {loja.nome} (instância {loja.instancia})', alvo=loja)
    messages.success(request, f'{loja.nome} cadastrada. Escaneie o QR Code com o WhatsApp desse número.')
    return redirect(f"{request.path}?qr={loja.pk}")


@login_required
def respostas_rapidas(request):
    editar = request.GET.get('editar')
    obj = RespostaRapida.objects.filter(pk=editar).first() if editar else None
    # Vindo da tela da conversa ("+ Nova"), volta para ela depois de salvar
    voltar = request.POST.get('voltar', '')
    if not url_has_allowed_host_and_scheme(voltar, allowed_hosts={request.get_host()}):
        voltar = ''
    if request.method == 'POST':
        if 'excluir' in request.POST:
            resp = get_object_or_404(RespostaRapida, pk=request.POST['excluir'])
            descricao = f'Excluiu a resposta rápida /{resp.atalho} ("{resp.titulo}")'
            resp.delete()
            auditoria.registrar(request, Tipo.CONFIGURACAO, 'resposta_rapida_excluir', descricao)
            messages.success(request, 'Resposta rápida excluída.')
            return redirect('painel:respostas_rapidas')
        form = RespostaRapidaForm(request.POST, instance=obj)
        if form.is_valid():
            mudancas = auditoria.mudancas_form(form) if obj else {}
            resp = form.save()
            if not obj:
                auditoria.registrar(
                    request, Tipo.CONFIGURACAO, 'resposta_rapida_criar',
                    f'Criou a resposta rápida /{resp.atalho} ("{resp.titulo}")', alvo=resp,
                )
            elif mudancas:
                auditoria.registrar(
                    request, Tipo.CONFIGURACAO, 'resposta_rapida_editar',
                    f'Editou a resposta rápida /{resp.atalho} ("{resp.titulo}")', alvo=resp, mudancas=mudancas,
                )
            messages.success(request, f'Resposta rápida salva. Use /{resp.atalho} no chat.')
            return redirect(voltar or 'painel:respostas_rapidas')
        if voltar:
            erros = ' '.join(' '.join(e) for e in form.errors.values())
            messages.error(request, f'Não foi possível salvar a resposta rápida. {erros}')
            return redirect(voltar)
    else:
        form = RespostaRapidaForm(instance=obj)
    return _render(request, 'painel/respostas_rapidas.html', {
        'secao': 'configuracoes', 'form': form, 'editando': obj,
        'respostas': RespostaRapida.objects.all(),
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
                titulo = cat.titulo
                cat.delete()
                auditoria.registrar(request, Tipo.ESTOQUE, 'categoria_excluir', f'Excluiu a categoria "{titulo}"')
                messages.success(request, 'Categoria excluída.')
            return redirect('painel:categorias')
        form = CategoriaForm(request.POST, instance=obj)
        if form.is_valid():
            mudancas = auditoria.mudancas_form(form) if obj else {}
            cat = form.save()
            if not obj:
                auditoria.registrar(
                    request, Tipo.ESTOQUE, 'categoria_criar', f'Criou a categoria "{cat.titulo}"', alvo=cat,
                )
            elif mudancas:
                auditoria.registrar(
                    request, Tipo.ESTOQUE, 'categoria_editar', f'Editou a categoria "{cat.titulo}"',
                    alvo=cat, mudancas=mudancas,
                )
            messages.success(request, 'Categoria salva.')
            return redirect('painel:categorias')
    else:
        form = CategoriaForm(instance=obj)
    return _render(request, 'painel/categorias.html', {
        'secao': 'estoque', 'form': form, 'editando': obj,
        'categorias': Categoria.objects.annotate(num_produtos=Count('produtos')),
    })


# ── Auditoria (só administradores) ──


@login_required
def auditoria_lista(request):
    if not request.user.is_superuser:
        raise PermissionDenied
    busca = request.GET.get('q', '').strip()
    tipo = request.GET.get('tipo', '')
    responsavel = request.GET.get('responsavel', '')
    de, ate = request.GET.get('de', ''), request.GET.get('ate', '')

    qs = RegistroAuditoria.objects.all()
    if tipo in RegistroAuditoria.Tipo.values:
        qs = qs.filter(tipo=tipo)
    if responsavel:
        qs = qs.filter(responsavel=responsavel)
    if busca:
        qs = qs.filter(Q(descricao__icontains=busca) | Q(alvo_id=busca.lstrip('#')))
    # O período segue o dia comercial, como os relatórios
    try:
        if de:
            qs = qs.filter(criado_em__gte=_inicio_dia(datetime.strptime(de, '%Y-%m-%d').date()))
        if ate:
            qs = qs.filter(criado_em__lt=_inicio_dia(datetime.strptime(ate, '%Y-%m-%d').date() + timedelta(days=1)))
    except ValueError:
        messages.error(request, 'Data inválida no filtro de período.')

    return _render(request, 'painel/auditoria.html', {
        'secao': 'auditoria',
        'pagina': Paginator(qs, 50).get_page(request.GET.get('pagina')),
        'busca': busca, 'tipo': tipo, 'responsavel': responsavel, 'de': de, 'ate': ate,
        'tipos': RegistroAuditoria.Tipo.choices,
        'responsaveis': (
            RegistroAuditoria.objects.order_by('responsavel')
            .values_list('responsavel', flat=True).distinct()
        ),
        'bot': RegistroAuditoria.BOT,
    })
