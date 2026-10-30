"""Vendas lançadas pela equipe no grupo de avisos do WhatsApp.

Quem vendeu fora do bot (no número pessoal, na rua, no fechamento com os
revendedores) manda no grupo um texto ou áudio começando com "venda", "saiu"
ou "lançar":

    "Saiu um Ignite V80 Blueberry Ice"
    "Lançar vendas do revendedor João: 3 Ignite V80 menta, 4 Elfbar 15k uva"

A IA entende modelo, sabor, quantidade, revendedor e número do cliente; o
código confere tudo no cardápio e no estoque (nome, preço e estoque nunca vêm
da IA) e o bot responde no grupo com o resumo. A saída só é lançada quando a
mesma pessoa responde "ok" (venda avulsa: "ok 11 98888-7777" com o número do
cliente, ou só "ok"). Até lá dá para corrigir mandando outra mensagem.

O rascunho fica no cache por 30 minutos, um por pessoa do grupo.
"""

import asyncio
import json
import logging
import re
import unicodedata
from datetime import timedelta
from decimal import Decimal

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from painel import auditoria, catalogo, lojas, servicos
from painel.models import (
    Categoria, Configuracao, Loja, PerfilEquipe, Pedido, Produto, Revendedor, Variante, precos_da_loja,
)

from .llm_service import _PARAMETROS_ITENS, _get_client, formatar_preco, transcrever_audio
from .notificacoes import baixar_midia, responder_no_grupo

logger = logging.getLogger(__name__)

_RASCUNHO_VALIDADE = timedelta(minutes=30)

# Palavras que chamam o bot no grupo (já sem acento), entre as primeiras da
# mensagem: "venda ...", "lançar ...", "acabou de sair ..."
_GATILHOS = {
    'venda', 'vendas', 'vendi', 'vendeu', 'vendemos', 'saiu', 'sairam', 'sair',
    'lancar', 'lanca', 'lance', 'lancamento',
}
_GATILHOS_FRACOS = {'saiu', 'sairam', 'sair'}
_GATILHO_ATE_PALAVRA = 4
_CONFIRMAR = {'ok', 'okay', 'confirmar', 'confirma', 'confirmo', 'confirmado', 'sim'}
_CANCELAR = {'cancelar', 'cancela', 'cancelado', 'descartar', 'descarta', 'esquece', 'nao'}
# "cancelar venda", "não, cancela": só essas palavras ("não, é menta" é correção)
_CANCELAR_COMPLEMENTO = _CANCELAR | {'venda', 'saida', 'tudo', 'isso', 'a', 'essa'}

_tasks: set[asyncio.Task] = set()
# Uma mensagem por vez de cada pessoa: o "ok" não passa na frente do pedido
_travas: dict[str, asyncio.Lock] = {}


def _normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize('NFKD', texto).encode('ascii', 'ignore').decode()
    return re.sub(r'\s+', ' ', re.sub(r'[^\w\s]', ' ', sem_acento.lower())).strip()


def _palavras(texto: str) -> list[str]:
    return _normalizar(texto).split()


def _gatilho(texto: str) -> str:
    """A palavra que chamou o bot ('' se a mensagem não é com ele)."""
    return next((p for p in _palavras(texto)[:_GATILHO_ATE_PALAVRA] if p in _GATILHOS), '')


def tem_gatilho(texto: str) -> bool:
    return bool(_gatilho(texto))


def _chave_rascunho(autor: str) -> str:
    return f'grupo-venda:{autor}'


# ── Entrada pelo webhook ──


async def receber(body: dict, instancia: str) -> None:
    """Mensagem de um grupo. Só a do grupo de avisos, de alguém da equipe
    (não do próprio bot), com a função ligada, vira tarefa em segundo plano."""
    data = body.get('data', body)
    key = data.get('key') or {}
    if key.get('fromMe'):
        return
    config = await Configuracao.aobter()
    if not (config.vendas_pelo_grupo and config.grupo_avisos_id) or key.get('remoteJid') != config.grupo_avisos_id:
        return
    # Com vários números da loja no grupo, a mesma mensagem chega por cada um
    if key.get('id') and not await cache.aadd(f'grupo-msg:{key["id"]}', 1, timeout=600):
        return

    mensagem = data.get('message') or {}
    texto = (
        mensagem.get('conversation') or (mensagem.get('extendedTextMessage') or {}).get('text') or ''
    ).strip()
    audio = 'audioMessage' in mensagem
    autor = _autor(data)
    if not autor or not (texto or audio):
        return
    # Texto sem gatilho e sem venda em andamento é conversa da equipe: nem chama a IA
    if texto and not tem_gatilho(texto) and not await cache.aget(_chave_rascunho(autor)):
        return

    task = asyncio.create_task(_processar(data, instancia, autor, texto, audio), name=f'grupo_{autor}')
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _autor(data: dict) -> str:
    """JID de quem mandou no grupo. Prefere o número de telefone; nas versões
    do WhatsApp com identificador anônimo (@lid) a Evolution manda o número
    num campo à parte."""
    key = data.get('key') or {}
    candidatos = [
        key.get('participantPn'), key.get('participantAlt'), key.get('participant'), data.get('participant'),
    ]
    candidatos = [c for c in candidatos if isinstance(c, str) and c]
    for jid in candidatos:
        if jid.endswith('@s.whatsapp.net'):
            return jid
    return candidatos[0] if candidatos else ''


async def _processar(data: dict, instancia: str, autor: str, texto: str, audio: bool) -> None:
    trava = _travas.setdefault(autor, asyncio.Lock())
    async with trava:
        try:
            if audio:
                midia = await baixar_midia(data, instancia)
                texto = await transcrever_audio(*midia) if midia else ''
                rascunho = await cache.aget(_chave_rascunho(autor))
                if not texto or (not rascunho and not tem_gatilho(texto)):
                    return
                logger.info('🎤 Áudio no grupo de %s: %s', autor, texto[:120])
            resposta = await _tratar(autor, data.get('pushName') or '', texto)
        except Exception:
            logger.exception('Erro ao tratar venda do grupo (%s)', autor)
            resposta = '⚠️ Não consegui processar essa venda agora. Tente de novo em instantes.'
        if resposta:
            await responder_no_grupo(data['key']['remoteJid'], resposta, citar=data)


async def _tratar(autor: str, nome: str, texto: str) -> str:
    """Resposta do bot no grupo ('' = não era com ele)."""
    chave = _chave_rascunho(autor)
    rascunho = await cache.aget(chave)
    palavras = _palavras(texto)

    if rascunho and palavras and not tem_gatilho(texto):
        so_numero = re.fullmatch(r'\+?[\d\s().-]{10,20}', texto.strip()) is not None
        if palavras[0] in _CONFIRMAR or so_numero:
            return await _confirmar(autor, nome, texto, rascunho)
        if palavras[0] in _CANCELAR and all(p in _CANCELAR_COMPLEMENTO for p in palavras):
            await cache.adelete(chave)
            return '🗑️ Saída descartada. Nada foi lançado.'

    pedido = await _interpretar(texto, rascunho['pedido'] if rascunho and not tem_gatilho(texto) else None)
    if pedido is None:
        # "saiu pra entrega" é conversa comum no grupo: só "venda"/"lançar" pedem resposta
        if tem_gatilho(texto) and _gatilho(texto) not in _GATILHOS_FRACOS:
            return 'Não entendi quais itens saíram. Mande de novo com modelo, sabor e quantidade.'
        return ''

    conferido = await sync_to_async(_conferir)(pedido)
    await cache.aset(chave, {
        'pedido': pedido, 'texto': texto, 'assinatura': _assinatura(conferido['itens']),
    }, timeout=int(_RASCUNHO_VALIDADE.total_seconds()))
    return _texto_resumo(nome, pedido, conferido)


# ── IA: o que foi dito → itens ──

_PROMPT = """Você ajuda a equipe da Robopod (loja de pods/vapes) a registrar saídas de estoque.
Uma pessoa da equipe escreveu no grupo do WhatsApp (ou mandou áudio, já transcrito) o que vendeu.

Chame `lancar_saida` com TODOS os itens citados:
- Use o nome do modelo e do sabor exatamente como no cardápio abaixo. A transcrição de áudio
  erra nomes ("ignaite" = Ignite, "blue berry" = Blueberry, "elf bar" = Elfbar): traduza.
- Quantidade por extenso vira número ("três" = 3; "um"/"uma" antes do produto = 1). Sem quantidade, 1.
- "3 ignite v80, sendo 2 menta e 1 uva" são dois itens: 2 menta e 1 uva.
- Sem sabor dito, omita o sabor: o sistema pergunta. Nunca invente item ou sabor.
- Venda para revendedor ("do revendedor João", "pro João revender", "fechamento do João"):
  preencha `revendedor` com o nome dito. Venda para cliente final: deixe vazio.
- Número do cliente dito na mensagem vai em `telefone_cliente` (só dígitos, com DDD).
- Loja da venda ("na loja 2", "pela Barra") vai em `loja`, com o nome da lista de lojas abaixo.
  Sem loja dita, deixe vazio.

Se houver uma VENDA EM ANDAMENTO, a mensagem pode corrigir ou completar essa venda
("na verdade foram 2", "o sabor é menta", "tira o elfbar", "mais um ignite uva"): devolva
a venda COMPLETA já corrigida, mantendo o que não mudou.

Chame `ignorar` se a mensagem não descreve venda nem corrige a venda em andamento
(conversa da equipe, "saiu para entrega" de um pedido do bot, dúvida etc.).

LOJAS: {lojas}

CARDÁPIO (modelo: sabores):
{cardapio}"""

_TOOLS = [
    {
        'type': 'function',
        'function': {
            'name': 'lancar_saida',
            'description': 'Monta a saída de estoque descrita pela pessoa da equipe.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'itens': _PARAMETROS_ITENS['properties']['itens'],
                    'revendedor': {
                        'type': 'string',
                        'description': 'Nome do revendedor, se a venda foi para revendedor. Vazio para cliente final.',
                    },
                    'telefone_cliente': {
                        'type': 'string', 'description': 'Número do cliente final, se foi dito (DDD + número)',
                    },
                    'nome_cliente': {'type': 'string', 'description': 'Nome do cliente final, se foi dito'},
                    'loja': {'type': 'string', 'description': 'Loja da venda, se foi dita. Vazio = loja principal'},
                },
                'required': ['itens'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'ignorar',
            'description': 'A mensagem não é uma venda nem corrige a venda em andamento.',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
]


def _cardapio_prompt() -> str:
    linhas = []
    for item in _catalogo_equipe():
        sabores = ', '.join(v['nome'] for v in item['variantes'])
        linhas.append(f"- {item['nome']}: {sabores}" if sabores else f"- {item['nome']}")
    return '\n'.join(linhas)


async def _interpretar(texto: str, em_andamento: dict | None) -> dict | None:
    """{'itens': [...], 'revendedor', 'telefone_cliente', 'nome_cliente', 'loja'}
    ou None se a mensagem não for venda."""
    cardapio = await sync_to_async(_cardapio_prompt)()
    nomes_lojas = ', '.join([lj.nome async for lj in Loja.objects.filter(ativa=True)]) or '-'
    mensagens = [{'role': 'system', 'content': _PROMPT.format(cardapio=cardapio, lojas=nomes_lojas)}]
    if em_andamento:
        mensagens.append({
            'role': 'system',
            'content': 'VENDA EM ANDAMENTO: ' + json.dumps(em_andamento, ensure_ascii=False),
        })
    mensagens.append({'role': 'user', 'content': texto})
    resposta = await _get_client().chat.completions.create(
        model=settings.OPENAI_MODEL,
        messages=mensagens,
        tools=_TOOLS,
        tool_choice='required',
        temperature=0,
    )
    chamadas = resposta.choices[0].message.tool_calls or []
    if not chamadas or chamadas[0].function.name != 'lancar_saida':
        return None
    try:
        args = json.loads(chamadas[0].function.arguments or '{}')
    except json.JSONDecodeError:
        logger.warning('Argumentos inválidos da IA no grupo: %s', chamadas[0].function.arguments)
        return None
    itens = [i for i in args.get('itens') or [] if isinstance(i, dict) and i.get('modelo')]
    if not itens:
        return None
    return {
        'itens': itens,
        'revendedor': str(args.get('revendedor') or '').strip()[:120],
        'telefone_cliente': str(args.get('telefone_cliente') or '').strip(),
        'nome_cliente': str(args.get('nome_cliente') or '').strip()[:120],
        'loja': str(args.get('loja') or '').strip()[:60],
    }


# ── Conferência no cardápio e no estoque ──


def _catalogo_equipe() -> list[dict]:
    """Produtos ativos com os sabores ativos, inclusive os sem estoque (a
    equipe precisa saber que acabou, e não receber "não achei")."""
    itens = []
    for cat in Categoria.objects.filter(ativo=True).prefetch_related('produtos__variantes'):
        for p in cat.produtos.all():
            if not p.ativo:
                continue
            itens.append({
                'nome': p.nome, 'produto': p, 'estoque': p.estoque,
                'variantes': [
                    {'nome': v.nome, 'variante': v, 'estoque': v.estoque}
                    for v in p.variantes.all() if v.ativo
                ],
            })
    return itens


def _localizar_revendedor(nome: str) -> Revendedor | None:
    opcoes = [{'nome': r.nome, 'revendedor': r} for r in Revendedor.objects.filter(ativo=True)]
    achado = catalogo.localizar_item(nome, opcoes) if nome and opcoes else None
    return achado['revendedor'] if achado else None


def _localizar_loja(nome: str) -> tuple[Loja | None, bool]:
    """(loja da venda, achou a loja dita). Sem loja dita (ou não achada), a principal."""
    if nome:
        opcoes = [{'nome': lj.nome, 'loja': lj} for lj in Loja.objects.filter(ativa=True)]
        achado = catalogo.localizar_item(nome, opcoes) if opcoes else None
        if achado:
            return achado['loja'], True
    return lojas.principal_sync(), not nome


def _quantidade(valor) -> int:
    try:
        return max(1, int(valor or 1))
    except (TypeError, ValueError):
        return 1


def _conferir(pedido: dict) -> dict:
    """Casa os itens ditos com o cardápio e checa o estoque.

    Returns:
        {'itens': linhas que podem sair, 'problemas': textos para a equipe,
         'revendedor': Revendedor | None (None também se for novo)}.
    """
    produtos = _catalogo_equipe()
    loja, loja_achada = _localizar_loja(pedido.get('loja') or '')
    precos = precos_da_loja(loja.pk if loja else None)
    para_revendedor = bool(pedido.get('revendedor'))
    revendedor = _localizar_revendedor(pedido['revendedor']) if para_revendedor else None
    linhas: dict[tuple, dict] = {}
    problemas = []
    for pedido_item in pedido['itens']:
        referencia = str(pedido_item.get('modelo') or '').strip()
        item = catalogo.localizar_item(referencia, produtos)
        if item is None:
            problemas.append(f'Não achei *{referencia}* no cardápio.')
            continue
        variante = None
        if item['variantes']:
            sabor = str(pedido_item.get('sabor') or '').strip()
            variante = catalogo.localizar_variante(sabor, item['variantes']) if sabor else None
            if variante is None:
                com_estoque = [v['nome'] for v in item['variantes'] if v['estoque'] is None or v['estoque'] > 0]
                problema = f"*{item['nome']}*: " + (f'não achei o sabor *{sabor}*.' if sabor else 'qual sabor?')
                problema += f" Com estoque: {', '.join(com_estoque)}." if com_estoque else ' Nenhum sabor com estoque.'
                problemas.append(problema)
                continue

        quantidade = _quantidade(pedido_item.get('quantidade'))
        chave = ('v', variante['variante'].pk) if variante else ('p', item['produto'].pk)
        if chave in linhas:
            linhas[chave]['quantidade'] += quantidade
            continue
        produto = item['produto']
        usa_revenda = para_revendedor and produto.preco_revenda is not None
        linhas[chave] = {
            'chave': chave,
            'produto': produto,
            'variante': variante['variante'] if variante else None,
            'nome': f"{produto.nome} - {variante['nome']}" if variante else produto.nome,
            'quantidade': quantidade,
            'preco': produto.preco_revenda if usa_revenda else precos.get(produto.pk, produto.preco),
            'sem_preco_revenda': para_revendedor and not usa_revenda,
            'estoque': (variante or item)['estoque'],
        }

    itens = []
    for linha in linhas.values():
        estoque = linha['estoque']
        if estoque is not None and linha['quantidade'] > estoque:
            problemas.append(
                f"*{linha['nome']}*: " + (
                    'sem estoque.' if estoque <= 0
                    else f"só tem {estoque} no estoque (você disse {linha['quantidade']})."
                ),
            )
        else:
            itens.append(linha)
    return {
        'itens': itens, 'problemas': problemas, 'revendedor': revendedor,
        'loja': loja, 'loja_achada': loja_achada,
        'varias_lojas': Loja.objects.filter(ativa=True).count() > 1,
    }


def _assinatura(itens: list[dict]) -> list:
    """O que vai sair, para conferir na hora do "ok" que nada mudou."""
    return sorted([*linha['chave'], linha['quantidade']] for linha in itens)


def _telefone_formatado(telefone: str) -> str:
    t = telefone
    if len(t) == 13:
        return f'({t[2:4]}) {t[4:9]}-{t[9:]}'
    if len(t) == 12:
        return f'({t[2:4]}) {t[4:8]}-{t[8:]}'
    return t


def _texto_resumo(nome: str, pedido: dict, conferido: dict) -> str:
    itens, problemas = conferido['itens'], conferido['problemas']
    linhas = [f"📝 *Conferir saída*{f' — {nome}' if nome else ''}"]
    if pedido['revendedor']:
        revendedor = conferido['revendedor']
        if revendedor:
            linhas.append(f'👤 Revendedor: *{revendedor.nome}* (preço de revenda, fica a receber)')
        else:
            linhas.append(f"👤 Revendedor: *{pedido['revendedor']}* — novo, será cadastrado ao confirmar")
    loja = conferido['loja']
    if conferido['varias_lojas'] and loja:
        if conferido['loja_achada']:
            linhas.append(f'🏪 Loja: *{loja.nome}*')
        else:
            linhas.append(f"🏪 Loja: *{loja.nome}* (não achei a loja *{pedido['loja']}*; para trocar, mande a correção)")
    linhas.append('')

    total = Decimal('0')
    for linha in itens:
        subtotal = linha['preco'] * linha['quantidade']
        total += subtotal
        texto = f"• {linha['quantidade']}× {linha['nome']} — {formatar_preco(subtotal)}"
        if linha['estoque'] is not None:
            texto += f" _(estoque {linha['estoque']} → {linha['estoque'] - linha['quantidade']})_"
        linhas.append(texto)
    if itens:
        linhas.append(f'*Total: {formatar_preco(total)}*')
    if any(linha['sem_preco_revenda'] for linha in itens):
        linhas.append('_Itens sem preço de revenda cadastrado saem pelo preço do cardápio._')

    if problemas:
        linhas += ['', '⚠️ *Ficou de fora:*'] if itens else ['⚠️ *Não deu para lançar:*']
        linhas += [f'• {p}' for p in problemas]

    linhas.append('')
    if not itens:
        linhas.append('Mande a correção (ex.: _o sabor é menta_) ou uma venda nova.')
        return '\n'.join(linhas)
    if pedido['revendedor']:
        linhas.append('Responda *ok* para lançar ou *cancelar*. Para corrigir, mande a correção.')
    else:
        telefone = servicos.normalizar_telefone(pedido['telefone_cliente'] or '')
        if telefone:
            linhas.append(f'📱 Cliente: {_telefone_formatado(telefone)}')
            linhas.append('Responda *ok* para lançar ou *cancelar*. Para corrigir, mande a correção.')
        else:
            linhas.append(
                'Responda *ok* com o número do cliente (ex.: _ok 11 98888-7777_), '
                'só *ok* para lançar sem número, ou *cancelar*.',
            )
    return '\n'.join(linhas)


# ── Confirmação: lança a venda e baixa o estoque ──


async def _confirmar(autor: str, nome: str, texto: str, rascunho: dict) -> str:
    pedido = rascunho['pedido']
    telefone = ''
    if not pedido['revendedor']:
        digitos = re.sub(r'\D', '', texto)
        telefone = servicos.normalizar_telefone(digitos or pedido['telefone_cliente'] or '')
        if telefone is None:
            if digitos:
                return 'Esse número não parece válido. Mande *ok* com DDD + número (ex.: _ok 11 98888-7777_).'
            telefone = ''  # o número que a IA anotou estava errado: lança sem

    usuario = await sync_to_async(_usuario_do_numero)(autor)
    resultado = await sync_to_async(_lancar)(rascunho, telefone, usuario, nome)
    if isinstance(resultado, dict):
        # O estoque ou o cardápio mudou desde o resumo: novo resumo para conferir
        rascunho['assinatura'] = _assinatura(resultado['itens'])
        await cache.aset(
            _chave_rascunho(autor), rascunho, timeout=int(_RASCUNHO_VALIDADE.total_seconds()),
        )
        if not resultado['itens']:
            return '⚠️ O estoque mudou e nada desta saída pode ser lançado agora.\n\n' + _texto_resumo(nome, pedido, resultado)
        return '⚠️ O estoque mudou desde o resumo. Confira de novo:\n\n' + _texto_resumo(nome, pedido, resultado)
    await cache.adelete(_chave_rascunho(autor))
    return resultado


def _usuario_do_numero(autor: str):
    """Usuário do painel com este WhatsApp (Configurações → Seu WhatsApp)."""
    if not autor.endswith('@s.whatsapp.net'):
        return None
    chave = servicos.chave_telefone(autor.split('@')[0])
    for perfil in PerfilEquipe.objects.select_related('usuario'):
        if perfil.usuario.is_active and servicos.chave_telefone(perfil.whatsapp) == chave:
            return perfil.usuario
    return None


def _lancar(rascunho: dict, telefone: str, usuario, nome: str) -> str | dict:
    """Lança a venda se o que vai sair é o mesmo do resumo. Senão devolve a
    conferência nova (dict) para a equipe conferir de novo."""
    pedido_ia = rascunho['pedido']
    with transaction.atomic():
        # Trava as linhas de estoque antes de conferir: dois "ok" ao mesmo tempo
        # não vendem a mesma unidade duas vezes
        variantes = [c[1] for c in rascunho['assinatura'] if c[0] == 'v']
        produtos = [c[1] for c in rascunho['assinatura'] if c[0] == 'p']
        list(Variante.objects.select_for_update().filter(pk__in=variantes))
        list(Produto.objects.select_for_update().filter(pk__in=produtos))

        conferido = _conferir(pedido_ia)
        if not conferido['itens'] or _assinatura(conferido['itens']) != rascunho['assinatura']:
            return conferido

        revendedor = conferido['revendedor']
        if pedido_ia['revendedor'] and revendedor is None:
            revendedor = Revendedor.objects.create(nome=pedido_ia['revendedor'])
        quem = (usuario.get_username() if usuario else '') or nome or 'equipe'
        pedido = servicos.registrar_venda_manual(
            [
                {
                    'produto': linha['produto'], 'variante': linha['variante'],
                    'quantidade': linha['quantidade'], 'preco_unitario': linha['preco'],
                }
                for linha in conferido['itens']
            ],
            status=Pedido.Status.ENTREGUE,
            criado_em=timezone.now(),
            telefone=telefone,
            nome=pedido_ia['nome_cliente'] if telefone else '',
            observacoes=f'Lançada no grupo por {quem}: "{rascunho["texto"][:400]}"',
            vendedor=usuario,
            origem=Pedido.Origem.GRUPO,
            revendedor=revendedor,
            loja=conferido['loja'],
        )

    n_itens = sum(linha['quantidade'] for linha in conferido['itens'])
    para = f' para o revendedor {revendedor.nome}' if revendedor else ''
    auditoria.registrar_equipe(
        usuario, nome, auditoria.Tipo.PEDIDO, 'venda_grupo',
        f'Lançou no grupo a venda #{pedido.pk}{para}: {n_itens} {"item" if n_itens == 1 else "itens"}, '
        f'total R$ {auditoria.texto(pedido.total)}',
        alvo=pedido,
    )
    return _texto_lancada(pedido, conferido['itens'], revendedor)


def _texto_lancada(pedido: Pedido, itens: list[dict], revendedor: Revendedor | None) -> str:
    linhas = [f'✅ *Venda #{pedido.pk} lançada*', '']
    baixos = []
    for linha in itens:
        texto = f"• {linha['quantidade']}× {linha['nome']}"
        if linha['estoque'] is not None:
            restante = linha['estoque'] - linha['quantidade']
            texto += f' _(ficou {restante})_'
            if restante <= linha['produto'].estoque_minimo:
                baixos.append(f"{linha['nome']} ({restante})")
        linhas.append(texto)
    linhas.append(f'*Total: {formatar_preco(pedido.total)}*')
    if revendedor:
        a_receber = sum(
            (p.total for p in revendedor.pedidos.filter(acertado_em__isnull=True).exclude(
                status=Pedido.Status.CANCELADO,
            )),
            Decimal('0'),
        )
        linhas.append(f'👤 {revendedor.nome} — a receber no total: *{formatar_preco(a_receber)}*')
    elif pedido.cliente_id:
        linhas.append(f'📱 Cliente: {_telefone_formatado(pedido.cliente.telefone)}')
    if baixos:
        linhas += ['', '📉 Estoque baixo: ' + ', '.join(baixos)]
    return '\n'.join(linhas)
