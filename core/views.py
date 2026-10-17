"""Webhook da Evolution API e roteamento da máquina de estados.

View assíncrona que recebe os webhooks POST da Evolution API,
identifica a sessão do cliente e roteia para o handler correto
com base no estado atual da sessão.
"""

import asyncio
import json
import logging
import re
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import SessaoChat
from .llm_service import (
    processar_mensagem, gerar_menu_texto, gerar_sabores_texto, localizar_item,
    gerar_secoes_lista, gerar_secoes_categoria, gerar_secoes_variantes,
    buscar_item, buscar_categoria, item_tem_variantes, formatar_preco, transcrever_audio,
    descrever_imagem,
)
from .notificacoes import (
    enviar_whatsapp, notificar_equipe, baixar_midia,
    enviar_lista_whatsapp, enviar_imagem_whatsapp, enviar_documento_whatsapp,
)
from painel import catalogo, servicos
from painel.models import Configuracao

from . import frete
from .frete import cotar_frete

logger = logging.getLogger(__name__)

# Referências às tasks de background — sem isso o asyncio pode descartar
# a task (garbage collection) antes de ela terminar.
_tasks_background: set[asyncio.Task] = set()

# Endereços não encontrados no mapa antes de passar para atendente
_MAX_TENTATIVAS_ENDERECO = 2

# Sem falar com o bot por mais que isso, a próxima mensagem abre uma conversa
# nova (boas-vindas + cardápio). A loja abre 18h-03h: cobre "voltou outra noite".
_NOVA_CONVERSA_APOS = timedelta(hours=4)

# Palavras de saudação pura: se a primeira mensagem só tiver essas palavras,
# respondemos apenas com boas-vindas + cardápio; senão ela também vai para a IA.
_PALAVRAS_SAUDACAO = {
    'oi', 'oii', 'oie', 'ola', 'olá', 'opa', 'eai', 'e', 'aí', 'ai', 'hey',
    'hello', 'boa', 'bom', 'noite', 'tarde', 'dia', 'tudo', 'bem', 'salve',
    'blz', 'beleza', 'td', 'tranquilo',
}


def _eh_saudacao(texto: str) -> bool:
    palavras = re.findall(r'\w+', texto.lower())
    return bool(palavras) and all(p in _PALAVRAS_SAUDACAO for p in palavras)

# IDs de botões convertidos em palavras-chave fora do atendimento LLM
_BOTOES_PARA_TEXTO = {
    'cancelar_pedido': 'cancelar',
    'alterar_pedido': 'alterar',
    'falar_humano': 'atendente',
}


def _extrair_dados_webhook(body: dict) -> tuple[str, str, str] | None:
    """Extrai telefone, texto e tipo de interação do payload da Evolution API.

    Returns:
        Tupla (telefone, texto, tipo_interacao) ou None se não for
        mensagem processável. tipo_interacao pode ser 'texto', 'lista',
        'botao', 'midia' (imagem/documento, ex.: comprovante) ou 'audio'
        (texto vazio: a transcrição é feita no webhook).
    """
    try:
        # Evolution API v2 payload structure
        data = body.get('data', body)
        key = data.get('key', {})
        telefone_raw = key.get('remoteJid', '')
        # Remove @s.whatsapp.net
        telefone = telefone_raw.split('@')[0] if '@' in telefone_raw else telefone_raw

        # Ignora mensagens enviadas pelo próprio bot e grupos
        if key.get('fromMe', False) or telefone_raw.endswith('@g.us'):
            return None

        message = data.get('message') or {}

        # ── Verifica respostas interativas primeiro ──
        # Resposta de lista (seleção de item)
        list_reply = (
            message.get('listResponseMessage', {})
            .get('singleSelectReply', {})
            .get('selectedRowId')
        )
        if list_reply and telefone:
            return telefone, list_reply, 'lista'

        # Resposta de botão
        button_reply = message.get('buttonsResponseMessage', {}).get(
            'selectedButtonId',
        )
        if not button_reply:
            button_reply = message.get('templateButtonReplyMessage', {}).get(
                'selectedId',
            )
        if button_reply and telefone:
            return telefone, button_reply, 'botao'

        # ── Imagem / documento (ex.: comprovante PIX) ──
        for tipo_midia, rotulo in (
            ('imageMessage', 'imagem'),
            ('documentMessage', 'documento'),
            ('documentWithCaptionMessage', 'documento'),
        ):
            if tipo_midia in message and telefone:
                legenda = (message[tipo_midia] or {}).get('caption') or ''
                return telefone, f'[{rotulo}] {legenda}'.strip(), 'midia'

        # ── Áudio / nota de voz: transcrito depois, no webhook ──
        if 'audioMessage' in message and telefone:
            return telefone, '', 'audio'

        # ── Mensagem de texto convencional ──
        texto = (
            message.get('conversation')
            or message.get('extendedTextMessage', {}).get('text')
            or ''
        )

        if not telefone or not texto.strip():
            return None

        return telefone, texto.strip(), 'texto'
    except Exception:
        logger.exception('Erro ao extrair dados do webhook')
        return None


async def _obter_ou_criar_sessao(telefone: str) -> SessaoChat:
    """Obtém ou cria uma sessão de chat para o telefone."""
    sessao, _ = await SessaoChat.objects.aget_or_create(
        telefone=telefone,
        defaults={
            'estado_atual': SessaoChat.Estado.ATENDIMENTO_LLM,
            'carrinho_temporario': {},
            'historico_mensagens': [],
        },
    )
    return sessao


def _resumo_itens(itens: list[dict]) -> tuple[str, float]:
    """Monta as linhas do resumo do carrinho e o subtotal."""
    linhas = [
        f"  • {i['quantidade']}x {i['nome']} — "
        f"{formatar_preco(i['preco_unitario'] * i['quantidade'])}"
        for i in itens
    ]
    subtotal = sum(i['preco_unitario'] * i['quantidade'] for i in itens)
    return '\n'.join(linhas), subtotal


async def _enviar_cardapio(telefone: str, rodape: bool = True) -> None:
    """Envia o cardápio: imagem opcional + lista interativa ou texto formatado."""
    if settings.CARDAPIO_IMAGEM_URL:
        await enviar_imagem_whatsapp(telefone, settings.CARDAPIO_IMAGEM_URL)

    if settings.WHATSAPP_MENU_INTERATIVO:
        enviado = await enviar_lista_whatsapp(
            telefone,
            titulo='📋 Cardápio Robopod',
            descricao='Escolha uma categoria para ver os produtos:',
            botao_texto='Ver categorias',
            secoes=gerar_secoes_lista(),
            rodape='Delivery Noturno 🌙',
        )
        if enviado:
            return
        logger.warning('Lista interativa falhou, enviando cardápio em texto')

    config = await Configuracao.aobter()
    await enviar_whatsapp(telefone, gerar_menu_texto(rodape=rodape, horario=config.horario_atendimento))


async def _enviar_pdf_cardapio(telefone: str) -> None:
    """Envia o PDF do cardápio, se houver um carregado em Painel > Configurações."""
    config = await Configuracao.aobter()
    if not config.cardapio_pdf:
        return

    def _ler() -> bytes:
        with config.cardapio_pdf.open('rb') as arquivo:
            return arquivo.read()

    try:
        conteudo = await sync_to_async(_ler)()
    except OSError:
        logger.exception('PDF do cardápio não encontrado: %s', config.cardapio_pdf.name)
        return
    await enviar_documento_whatsapp(
        telefone, conteudo, 'Cardápio Robopod.pdf', '📄 Nosso cardápio completo',
    )


_BOAS_VINDAS = (
    'Olá! 👋 Bem-vindo ao *Robopod* — Assistente Virtual da *Vape Rio Delivery*! 🌙\n\n'
    'Aqui embaixo vai nosso cardápio com os modelos e preços. '
    'É só me dizer qual modelo você quer que eu te mostro os sabores 😉\n\n'
    '🙋 Prefere falar com uma pessoa? Digite *atendente* a qualquer momento.'
)


async def _enviar_boas_vindas(telefone: str) -> None:
    """Primeiro contato: boas-vindas (com a opção de atendente), PDF e modelos."""
    await enviar_whatsapp(telefone, _BOAS_VINDAS)
    await _enviar_pdf_cardapio(telefone)
    # Sem o rodapé de instruções: as boas-vindas logo acima já explicam
    await _enviar_cardapio(telefone, rodape=False)


async def _transferir_para_humano(sessao: SessaoChat, motivo: str) -> None:
    """Coloca a sessão em atendimento humano, avisa o cliente e a equipe."""
    sessao.estado_atual = SessaoChat.Estado.ATENDIMENTO_HUMANO
    sessao.ultima_interacao = timezone.now()
    await sessao.asave(update_fields=['estado_atual', 'ultima_interacao'])
    await enviar_whatsapp(
        sessao.telefone,
        '🔄 Transferindo para um atendente humano... '
        'Aguarde um momento, por favor!',
    )
    await notificar_equipe(
        f'🚨 *Transferência para humano*\n'
        f'📱 Cliente: {sessao.telefone}\n'
        f'📝 Motivo: {motivo}\n'
        f'🕐 {timezone.localtime().strftime("%d/%m/%Y %H:%M")}',
        telefone=sessao.telefone,
    )


# ── Handlers de interação interativa ──────────────────────────────


async def _handler_selecao_lista(sessao: SessaoChat, row_id: str) -> None:
    """Processa seleção na lista interativa (categoria, item ou variante)."""
    # Nível 1: categoria -> envia a lista de itens dela
    if row_id.startswith('cat:'):
        categoria = buscar_categoria(row_id[4:])
        secoes = gerar_secoes_categoria(row_id[4:])
        if not categoria or not secoes:
            await enviar_whatsapp(sessao.telefone, '😕 Categoria não encontrada. Tente novamente!')
            return
        await enviar_lista_whatsapp(
            sessao.telefone,
            titulo=categoria['titulo'],
            descricao='Escolha um produto:',
            botao_texto='Ver produtos',
            secoes=secoes,
        )
        return

    # Nível 3: variante do item selecionado anteriormente
    pendente = sessao.carrinho_temporario.get('_selecao_pendente')
    if pendente:
        item_pendente = buscar_item(pendente)
        variantes = {v['id']: v for v in (item_pendente or {}).get('variantes', [])}
        if row_id in variantes:
            await _handler_selecao_variante(sessao, item_pendente, variantes[row_id])
            return

    # Nível 2: item
    item = buscar_item(row_id)
    if not item:
        await enviar_whatsapp(sessao.telefone, '😕 Item não encontrado. Tente novamente!')
        return

    legenda = f"*{item['nome']}* — {formatar_preco(item['preco'])}"
    if item.get('imagem_url'):
        await enviar_imagem_whatsapp(sessao.telefone, item['imagem_url'], legenda)

    if len(item['variantes']) > 10:
        # A lista do WhatsApp corta em 10 linhas: manda os sabores em texto
        # e o cliente responde escrevendo o sabor (segue pela IA).
        await enviar_whatsapp(sessao.telefone, gerar_sabores_texto(item))
        sessao.historico_mensagens.append({
            'role': 'assistant',
            'content': f"[Sabores de {item['nome']} enviados ao cliente]",
        })
        await sessao.asave(update_fields=['historico_mensagens'])
    elif item_tem_variantes(row_id):
        await enviar_lista_whatsapp(
            sessao.telefone,
            titulo=item['nome'],
            descricao='Escolha o sabor/cor desejado:',
            botao_texto='Escolher',
            secoes=gerar_secoes_variantes(row_id),
            rodape=formatar_preco(item['preco']),
        )
        # Salva estado de seleção pendente no carrinho_temporario
        sessao.carrinho_temporario['_selecao_pendente'] = row_id
        await sessao.asave(update_fields=['carrinho_temporario'])
    else:
        await _handler_atendimento_llm(sessao, f"Quero adicionar {item['nome']} ao pedido")


async def _handler_selecao_variante(
    sessao: SessaoChat, item: dict, variante: dict,
) -> None:
    """Processa seleção de variante (sabor/cor) de um item."""
    if variante.get('imagem_url'):
        await enviar_imagem_whatsapp(
            sessao.telefone,
            variante['imagem_url'],
            f"*{item['nome']}* — {variante['nome']}",
        )

    sessao.carrinho_temporario.pop('_selecao_pendente', None)
    await sessao.asave(update_fields=['carrinho_temporario'])

    await _handler_atendimento_llm(
        sessao, f"Quero adicionar {item['nome']} - {variante['nome']} ao pedido",
    )


async def _handler_botao(sessao: SessaoChat, botao_id: str) -> None:
    """Processa clique em botão interativo (estado de atendimento LLM)."""
    if botao_id == 'ver_cardapio':
        await _enviar_cardapio(sessao.telefone)
    elif botao_id == 'falar_humano':
        await _transferir_para_humano(sessao, 'Cliente solicitou via botão')
    elif botao_id == 'cancelar_pedido':
        sessao.resetar_sessao()
        await sessao.asave()
        await enviar_whatsapp(sessao.telefone, '❌ Pedido cancelado. Se precisar de algo, é só chamar! 😊')
    else:
        # Botão desconhecido — trata como texto
        await _handler_atendimento_llm(sessao, botao_id)


# ── Handlers de estado ────────────────────────────────────────────


async def _handler_atendimento_llm(
    sessao: SessaoChat, texto: str,
) -> None:
    """Processa mensagem no estado de atendimento via LLM."""
    # Voltou depois de muito tempo (ex.: na noite seguinte): conversa nova,
    # com boas-vindas e cardápio de novo em vez de seguir o papo antigo.
    if (
        sessao.historico_mensagens
        and timezone.now() - sessao.ultima_interacao > _NOVA_CONVERSA_APOS
    ):
        sessao.resetar_sessao()

    # "atendente" é anunciado nas boas-vindas: vai direto, sem passar pela IA
    if _normalizar(texto) in ('atendente', 'humano'):
        await _transferir_para_humano(sessao, 'Cliente digitou atendente')
        return

    # Primeira mensagem da sessão — boas-vindas + cardápio
    if not sessao.historico_mensagens:
        await _enviar_boas_vindas(sessao.telefone)
        historico_inicial = [
            {
                'role': 'assistant',
                'content': _BOAS_VINDAS + '\n[Cardápio com os modelos e preços enviado ao cliente]',
            },
        ]

        # "Oi" / "boa noite": já respondemos. Se o cliente já pediu algo
        # na primeira mensagem, segue para a IA com as boas-vindas no histórico.
        if _eh_saudacao(texto):
            sessao.historico_mensagens = [{'role': 'user', 'content': texto}, *historico_inicial]
            sessao.ultima_interacao = timezone.now()
            await sessao.asave(update_fields=['historico_mensagens', 'ultima_interacao'])
            return
        sessao.historico_mensagens = historico_inicial

    resultado, novo_historico = await processar_mensagem(
        historico=sessao.historico_mensagens,
        mensagem_usuario=texto,
    )

    sessao.historico_mensagens = novo_historico
    sessao.ultima_interacao = timezone.now()

    if resultado.tipo == 'texto':
        await enviar_whatsapp(sessao.telefone, resultado.conteudo)
        await sessao.asave(
            update_fields=['historico_mensagens', 'ultima_interacao'],
        )

    elif resultado.tipo == 'mostrar_cardapio':
        await sessao.asave(
            update_fields=['historico_mensagens', 'ultima_interacao'],
        )
        await _enviar_cardapio(sessao.telefone)

    elif resultado.tipo == 'mostrar_sabores':
        await sessao.asave(
            update_fields=['historico_mensagens', 'ultima_interacao'],
        )
        itens = [localizar_item(m) for m in resultado.dados.get('modelos', [])]
        itens = list({i['id']: i for i in itens if i}.values())  # sem repetidos
        if not itens:
            await enviar_whatsapp(
                sessao.telefone,
                '😕 Não achei esse modelo no cardápio. Pode me dizer o nome ou o número dele?',
            )
            return
        for item in itens:
            await enviar_whatsapp(sessao.telefone, gerar_sabores_texto(item))

    elif resultado.tipo == 'fechar_pedido':
        itens = resultado.dados.get('itens', [])
        if not itens:
            await sessao.asave(update_fields=['historico_mensagens', 'ultima_interacao'])
            await enviar_whatsapp(
                sessao.telefone, '😕 Não entendi os itens do pedido. Pode repetir o que deseja?',
            )
            return

        # Preço vem do banco (não da IA) e o estoque é conferido
        itens, problemas = await catalogo.validar_itens(itens)
        if problemas:
            aviso = (
                f"😕 Ops: {'; '.join(problemas)}.\n"
                'Quer ajustar o pedido? Me diga o que prefere.'
            )
            sessao.historico_mensagens.append({'role': 'assistant', 'content': aviso})
            await sessao.asave(update_fields=['historico_mensagens', 'ultima_interacao'])
            await enviar_whatsapp(sessao.telefone, aviso)
            return

        sessao.carrinho_temporario = {'itens': itens}
        sessao.estado_atual = SessaoChat.Estado.AGUARDANDO_ENDERECO
        await sessao.asave(update_fields=[
            'historico_mensagens', 'ultima_interacao',
            'carrinho_temporario', 'estado_atual',
        ])

        resumo, subtotal = _resumo_itens(itens)
        await enviar_whatsapp(
            sessao.telefone,
            f"✅ *Pedido anotado!*\n\n"
            f"{resumo}\n\n"
            f"🛍️ Subtotal: *{formatar_preco(subtotal)}*\n\n"
            f"📍 Agora me envie seu *endereço completo* para calcular o frete:\n"
            f"_Rua, número, bairro e cidade (e complemento, se tiver)_\n\n"
            f"Para mudar o pedido digite *alterar*; para desistir, *cancelar*.",
        )

    elif resultado.tipo == 'solicitar_humano':
        await sessao.asave(update_fields=['historico_mensagens', 'ultima_interacao'])
        await _transferir_para_humano(
            sessao, resultado.dados.get('motivo', 'Não especificado'),
        )


def _normalizar(texto: str) -> str:
    return texto.strip().lower().strip('.!* ')


async def _handler_aguardando_endereco(
    sessao: SessaoChat, texto: str,
) -> None:
    """Recebe endereço do cliente e dispara cotação de frete em background."""
    comando = _normalizar(texto)

    if comando in ('cancelar', 'cancela'):
        sessao.resetar_sessao()
        await sessao.asave()
        await enviar_whatsapp(sessao.telefone, '❌ Pedido cancelado. Se precisar de algo, é só chamar! 😊')
        return

    if comando in ('alterar', 'altera', 'mudar'):
        sessao.estado_atual = SessaoChat.Estado.ATENDIMENTO_LLM
        sessao.carrinho_temporario = {}
        sessao.ultima_interacao = timezone.now()
        await sessao.asave(update_fields=['estado_atual', 'carrinho_temporario', 'ultima_interacao'])
        await enviar_whatsapp(sessao.telefone, '✏️ Ok! Me diga o que deseja alterar no pedido.')
        return

    if comando in ('atendente', 'humano'):
        await _transferir_para_humano(sessao, 'Cliente pediu atendente ao informar endereço')
        return

    # Validação mínima: endereço precisa de rua + número (ou "s/n")
    if len(texto.strip()) < 10 or not re.search(r'\d|\bs/?n\b', texto, re.IGNORECASE):
        await enviar_whatsapp(
            sessao.telefone,
            '📍 Preciso do endereço completo, com *rua, número, bairro e cidade*.\n'
            '_Ex.: Rua das Flores, 120, Centro, Campinas_',
        )
        return

    sessao.estado_atual = SessaoChat.Estado.CALCULANDO_FRETE
    sessao.ultima_interacao = timezone.now()
    await sessao.asave(update_fields=['estado_atual', 'ultima_interacao'])

    await enviar_whatsapp(
        sessao.telefone,
        '🔍 Calculando o frete para o seu endereço... Aguarde um momento!',
    )

    # Dispara a cotação em background para não bloquear o webhook
    task = asyncio.create_task(
        _task_calcular_frete(sessao.telefone, texto.strip()),
        name=f'frete_{sessao.telefone}',
    )
    _tasks_background.add(task)
    task.add_done_callback(_tasks_background.discard)


async def _task_calcular_frete(telefone: str, endereco: str) -> None:
    """Task de background: cota frete, soma totais e envia PIX."""
    try:
        resultado_frete = await cotar_frete(endereco)

        # Busca sessão atualizada (o cliente pode ter mudado algo no meio tempo)
        sessao = await SessaoChat.objects.aget(telefone=telefone)
        if sessao.estado_atual != SessaoChat.Estado.CALCULANDO_FRETE:
            logger.info('Sessão %s saiu de calculando_frete, descartando cotação', telefone)
            return

        if not resultado_frete['sucesso']:
            motivo = resultado_frete.get('motivo')
            tentativas = sessao.carrinho_temporario.get('tentativas_endereco', 0) + 1
            sessao.carrinho_temporario['tentativas_endereco'] = tentativas
            sessao.carrinho_temporario['endereco'] = endereco

            # Endereço não achado no mapa: pede de novo antes de chamar humano
            if motivo == frete.NAO_ENCONTRADO and tentativas < _MAX_TENTATIVAS_ENDERECO:
                sessao.estado_atual = SessaoChat.Estado.AGUARDANDO_ENDERECO
                await sessao.asave(update_fields=['estado_atual', 'carrinho_temporario'])
                await enviar_whatsapp(
                    telefone,
                    '📍 Não encontrei esse endereço no mapa. Pode enviar de novo com '
                    '*rua, número, bairro e cidade*?\n'
                    '_Ex.: Rua das Flores, 120, Centro, Rio de Janeiro_\n\n'
                    'Se preferir, digite *atendente*.',
                )
                return

            # Frete falhou — escala para humano
            sessao.estado_atual = SessaoChat.Estado.ATENDIMENTO_HUMANO
            await sessao.asave(update_fields=['estado_atual', 'carrinho_temporario'])
            if motivo == frete.FORA_AREA:
                aviso = (
                    '📍 Esse endereço fica fora da nossa área de entrega automática. '
                    'Um atendente vai verificar se conseguimos te atender!'
                )
            else:
                aviso = (
                    '😕 Não consegui calcular o frete automaticamente para esse endereço. '
                    'Um atendente vai te ajudar em instantes!'
                )
            await enviar_whatsapp(telefone, aviso)
            resumo, subtotal = _resumo_itens(sessao.carrinho_temporario.get('itens', []))
            await notificar_equipe(
                f'⚠️ *Falha na cotação de frete*\n'
                f'📱 Cliente: {telefone}\n'
                f'📍 Endereço: {endereco}\n'
                f'🛍️ Pedido:\n{resumo}\n'
                f'💰 Subtotal: {formatar_preco(subtotal)}\n'
                f'❌ Motivo: {resultado_frete["mensagem"]}',
                telefone=telefone,
            )
            return

        # Calcula total
        valor_frete = resultado_frete['valor_frete']
        itens = sessao.carrinho_temporario.get('itens', [])
        resumo, subtotal = _resumo_itens(itens)
        total = subtotal + valor_frete

        sessao.carrinho_temporario['endereco'] = endereco
        pedido = await servicos.criar_pedido(telefone, sessao.carrinho_temporario, resultado_frete)

        # Atualiza sessão
        sessao.estado_atual = SessaoChat.Estado.AGUARDANDO_COMPROVANTE
        sessao.carrinho_temporario['pedido_id'] = pedido.pk if pedido else None
        sessao.carrinho_temporario['frete'] = valor_frete
        sessao.carrinho_temporario['total'] = total
        sessao.carrinho_temporario['endereco'] = endereco
        sessao.carrinho_temporario['frete_fornecedor'] = resultado_frete.get('fornecedor', '')
        sessao.carrinho_temporario['endereco_mapa'] = resultado_frete.get('endereco_formatado', '')
        sessao.carrinho_temporario.pop('tentativas_endereco', None)
        sessao.ultima_interacao = timezone.now()
        await sessao.asave(update_fields=[
            'estado_atual', 'carrinho_temporario', 'ultima_interacao',
        ])

        # Mostra como o endereço foi entendido no mapa para o cliente conferir
        endereco_mapa = resultado_frete.get('endereco_formatado')
        mapa = f"🗺️ _Localizado como: {endereco_mapa}_\n" if endereco_mapa else ''

        mensagem_cobranca = (
            f"🧾 *Resumo do Pedido*\n\n"
            f"{resumo}\n\n"
            f"🛍️ Subtotal: {formatar_preco(subtotal)}\n"
            f"🛵 Frete ({resultado_frete['mensagem']}): {formatar_preco(valor_frete)}\n"
            f"💰 *TOTAL: {formatar_preco(total)}*\n\n"
            f"📍 Entrega em: {endereco}\n"
            f"{mapa}\n"
            f"────────────────────\n"
            f"💳 *Pagamento via PIX*\n\n"
            f"Chave PIX: `{settings.CHAVE_PIX}`\n\n"
            f"Após o pagamento, envie a *foto ou PDF do comprovante* aqui. ✅\n"
            f"_Endereço errado ou alguma dúvida? Digite *atendente*._"
        )

        await enviar_whatsapp(telefone, mensagem_cobranca)

        # Notifica a equipe no grupo do WhatsApp (o número do pedido só vai para a equipe)
        numero_pedido = f' #{pedido.pk}' if pedido else ''
        await notificar_equipe(
            f'🛒 *Novo pedido{numero_pedido} aguardando pagamento*\n'
            f'📱 Cliente: {telefone}\n'
            f'📍 Endereço: {endereco}\n'
            f'🗺️ No mapa: {endereco_mapa or "?"}\n'
            f'🛍️ Pedido:\n{resumo}\n'
            f'🛵 Frete: {formatar_preco(valor_frete)} ({resultado_frete["mensagem"]})\n'
            f'🏷️ Cotado via: {resultado_frete.get("fornecedor", "?")}\n'
            f'💰 Total: {formatar_preco(total)}',
            telefone=telefone,
        )

    except Exception:
        logger.exception('Erro na task de cálculo de frete para %s', telefone)
        try:
            # Não deixa o cliente preso em "calculando_frete"
            await SessaoChat.objects.filter(telefone=telefone).aupdate(
                estado_atual=SessaoChat.Estado.ATENDIMENTO_HUMANO,
            )
            await enviar_whatsapp(
                telefone,
                '😕 Ocorreu um erro ao calcular o frete. Um atendente irá ajudá-lo em breve!',
            )
            await notificar_equipe(
                f'⚠️ *Erro na cotação de frete*\n'
                f'📱 Cliente: {telefone}\n'
                f'📍 Endereço: {endereco}\n'
                f'Veja os logs do app.',
                telefone=telefone,
            )
        except Exception:
            logger.exception('Falha ao tratar erro da task de frete')


async def _handler_calculando_frete(sessao: SessaoChat, texto: str) -> None:
    await enviar_whatsapp(sessao.telefone, '⏳ Ainda estou calculando o frete... Aguarde!')


async def _handler_aguardando_comprovante(
    sessao: SessaoChat, texto: str,
) -> None:
    """Recebe o comprovante (imagem/PDF) ou lembra o cliente de enviá-lo."""
    if texto.startswith('[imagem]') or texto.startswith('[documento]'):
        sessao.estado_atual = SessaoChat.Estado.ATENDIMENTO_HUMANO
        sessao.ultima_interacao = timezone.now()
        await sessao.asave(update_fields=['estado_atual', 'ultima_interacao'])
        await servicos.marcar_comprovante(sessao.carrinho_temporario.get('pedido_id'))
        # Sem resposta automática: a conversa passa direto para o atendimento,
        # que confirma o pagamento avançando o status do pedido.
        total = sessao.carrinho_temporario.get('total')
        await notificar_equipe(
            f'💸 *Comprovante recebido — conferir pagamento e chamar a entrega*\n'
            f'📱 Cliente: {sessao.telefone}\n'
            f'📍 Endereço: {sessao.carrinho_temporario.get("endereco", "?")}\n'
            f'💰 Total: {formatar_preco(total) if total is not None else "?"}',
            telefone=sessao.telefone,
        )
        return

    if _normalizar(texto) in ('atendente', 'humano'):
        await _transferir_para_humano(sessao, 'Cliente pediu atendente no pagamento')
        return

    await enviar_whatsapp(
        sessao.telefone,
        '⏳ Ainda estamos aguardando o comprovante de pagamento PIX.\n'
        f'Chave PIX: `{settings.CHAVE_PIX}`\n\n'
        'Envie a foto ou PDF do comprovante aqui. '
        'Se precisar de ajuda, digite *atendente*.',
    )


async def _handler_atendimento_humano(
    sessao: SessaoChat, texto: str,
) -> None:
    """Processa mensagem quando a sessão está em atendimento humano.

    Permite que o cliente volte ao atendimento automático digitando
    'voltar', 'menu' ou 'reiniciar'. Caso contrário, avisa que está
    com um humano e repassa ao grupo da equipe.
    """
    comando = _normalizar(texto)

    # Permite o cliente voltar ao bot a qualquer momento
    if comando in ('voltar', 'menu', 'reiniciar', 'bot', 'cardapio', 'cardápio'):
        sessao.resetar_sessao()
        await sessao.asave()
        await enviar_whatsapp(
            sessao.telefone,
            '🤖 Voltamos ao atendimento automático! Como posso te ajudar?',
        )
        await _enviar_cardapio(sessao.telefone)
        return

    # Repassa ao grupo da equipe (se configurado)
    await notificar_equipe(
        f'💬 *Mensagem do cliente (atendimento humano)*\n'
        f'📱 {sessao.telefone}: {texto}',
        telefone=sessao.telefone,
        mensagem_de_cliente=True,
    )

    # Avisa o cliente que está com um humano — no máximo a cada 15 min e
    # nunca enquanto um atendente estiver respondendo pelo painel
    if await servicos.atendente_falou_recentemente(sessao.telefone):
        return
    if await servicos.aviso_humano_recente(sessao.telefone, _AVISO_HUMANO):
        return
    await enviar_whatsapp(sessao.telefone, _AVISO_HUMANO)


_AVISO_HUMANO = (
    '⏳ Sua conversa está com um atendente humano. '
    'Aguarde a resposta!\n\n'
    '_Se quiser voltar ao atendimento automático, digite *voltar*._'
)


# Com a loja fechada, o mesmo cliente só recebe o aviso de novo depois disso
_REPETIR_AVISO_FECHADO_APOS = timedelta(hours=3)


async def _loja_fechada_respondeu(telefone: str) -> bool:
    """Expediente encerrado no painel: responde com a mensagem de loja fechada.

    Pedidos em andamento (endereço, frete, comprovante) e conversas com
    atendente seguem normalmente, para ninguém ficar no meio do caminho.
    Retorna True se a mensagem já foi tratada aqui.
    """
    config = await Configuracao.aobter()
    if config.expediente_aberto:
        return False
    sessao = await SessaoChat.objects.filter(telefone=telefone).afirst()
    if sessao is not None and sessao.estado_atual != SessaoChat.Estado.ATENDIMENTO_LLM:
        return False

    texto = config.texto_fechado
    desde = timezone.now() - _REPETIR_AVISO_FECHADO_APOS
    if config.expediente_fechado_em and config.expediente_fechado_em > desde:
        desde = config.expediente_fechado_em
    if not await servicos.bot_enviou_desde(telefone, texto, desde):
        await enviar_whatsapp(telefone, texto)
    return True


async def _bot_pausado_tratou(telefone: str, texto: str, tipo_interacao: str) -> bool:
    """Bot pausado no painel: nenhuma resposta automática, nem a de loja fechada.

    A mensagem já ficou registrada para o atendente responder pelo painel.
    Só o comprovante de pagamento segue o fluxo, que é silencioso (marca o
    pedido e avisa a equipe). Retorna True se a mensagem já foi tratada aqui.
    """
    config = await Configuracao.aobter()
    if not config.bot_pausado:
        return False
    if tipo_interacao == 'midia':
        sessao = await SessaoChat.objects.filter(telefone=telefone).afirst()
        if sessao is not None and sessao.estado_atual == SessaoChat.Estado.AGUARDANDO_COMPROVANTE:
            await _handler_aguardando_comprovante(sessao, texto)
    return True


async def _transcrever_audio(data: dict) -> str:
    """Baixa a nota de voz e devolve o texto falado ('' se não conseguir)."""
    midia = await baixar_midia(data)
    if midia is None:
        return ''
    return await transcrever_audio(*midia)


async def _descrever_imagem(data: dict) -> str:
    """Baixa a foto do cliente e devolve o que há nela ('' se não conseguir)."""
    midia = await baixar_midia(data)
    if midia is None:
        return ''
    return await descrever_imagem(*midia)


# ── Mapa de handlers por estado ──
_HANDLERS = {
    SessaoChat.Estado.ATENDIMENTO_LLM: _handler_atendimento_llm,
    SessaoChat.Estado.AGUARDANDO_ENDERECO: _handler_aguardando_endereco,
    SessaoChat.Estado.CALCULANDO_FRETE: _handler_calculando_frete,
    SessaoChat.Estado.AGUARDANDO_COMPROVANTE: _handler_aguardando_comprovante,
    SessaoChat.Estado.ATENDIMENTO_HUMANO: _handler_atendimento_humano,
}


@csrf_exempt
@require_POST
async def evolution_webhook(request) -> JsonResponse:
    """Endpoint principal do webhook da Evolution API.

    Recebe POST com o payload da mensagem, identifica o estado
    da sessão e roteia para o handler apropriado.
    """
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'JSON inválido'}, status=400)

    # Filtra apenas eventos de mensagem recebida
    evento = body.get('event', '')
    if evento not in ('messages.upsert', 'MESSAGES_UPSERT', ''):
        return JsonResponse({'status': 'evento ignorado'})

    dados = _extrair_dados_webhook(body)
    if dados is None:
        return JsonResponse({'status': 'mensagem ignorada'})

    telefone, texto, tipo_interacao = dados

    # Áudio: transcreve e segue como se o cliente tivesse escrito
    texto_painel = texto
    if tipo_interacao == 'audio':
        texto = await _transcrever_audio(body.get('data', body))
        texto_painel = f'🎤 Áudio: {texto}' if texto else '🎤 Áudio (não foi possível transcrever)'
    logger.info('📩 Mensagem de %s [%s]: %s', telefone, tipo_interacao, texto_painel[:100])

    # Histórico e cadastro do cliente para o painel
    nome_whatsapp = (body.get('data') or {}).get('pushName') or ''
    try:
        cliente = await servicos.registrar_mensagem_cliente(telefone, texto_painel, nome_whatsapp)
    except Exception:
        logger.exception('Falha ao registrar mensagem de %s', telefone)
        cliente = None
    if cliente is not None and cliente.bloqueado:
        logger.info('Cliente %s bloqueado, mensagem ignorada', telefone)
        return JsonResponse({'status': 'cliente bloqueado'})

    if await _bot_pausado_tratou(telefone, texto, tipo_interacao):
        return JsonResponse({'status': 'bot pausado'})

    if await _loja_fechada_respondeu(telefone):
        return JsonResponse({'status': 'loja fechada'})

    await catalogo.garantir_atualizado()

    # Obtém ou cria sessão
    sessao = await _obter_ou_criar_sessao(telefone)
    logger.info('📋 Sessão %s — estado: %s', telefone, sessao.estado_atual)
    em_atendimento_llm = sessao.estado_atual == SessaoChat.Estado.ATENDIMENTO_LLM

    # ── Respostas interativas no atendimento LLM ──
    if tipo_interacao == 'lista' and em_atendimento_llm:
        await _handler_selecao_lista(sessao, texto)
        return JsonResponse({'status': 'ok'})

    if tipo_interacao == 'botao' and em_atendimento_llm:
        await _handler_botao(sessao, texto)
        return JsonResponse({'status': 'ok'})

    # Foto no atendimento: a IA descreve e segue como se o cliente tivesse escrito
    if tipo_interacao == 'midia' and em_atendimento_llm and texto.startswith('[imagem]'):
        descricao = await _descrever_imagem(body.get('data', body))
        if descricao:
            legenda = texto.removeprefix('[imagem]').strip()
            texto = f'[Cliente enviou uma foto: {descricao}] {legenda}'.strip()
            tipo_interacao = 'texto'

    # Documento (ou foto que não deu para ler) fora da etapa de pagamento
    if tipo_interacao == 'midia' and em_atendimento_llm:
        await enviar_whatsapp(
            telefone,
            'Por enquanto só consigo ler mensagens de texto 🙂 '
            'Me diga o que deseja ou digite *atendente*.',
        )
        return JsonResponse({'status': 'ok'})

    if tipo_interacao == 'audio' and not texto:
        if sessao.estado_atual == SessaoChat.Estado.ATENDIMENTO_HUMANO:
            texto = texto_painel  # o atendente ouve no WhatsApp; o grupo é avisado
        else:
            await enviar_whatsapp(
                telefone,
                '😕 Não consegui ouvir seu áudio. Pode me mandar por escrito? '
                'Se preferir, digite *atendente*.',
            )
            return JsonResponse({'status': 'ok'})

    # Fora do atendimento LLM, botões viram palavras-chave (ex.: cancelar)
    if tipo_interacao in ('botao', 'lista'):
        texto = _BOTOES_PARA_TEXTO.get(texto, texto)

    # ── Tratamento de texto normal via máquina de estados ──
    handler = _HANDLERS.get(sessao.estado_atual)
    if handler:
        await handler(sessao, texto)
    else:
        logger.warning('Estado desconhecido: %s', sessao.estado_atual)

    # Retorna 200 imediato (obrigatório para webhooks)
    return JsonResponse({'status': 'ok'})
