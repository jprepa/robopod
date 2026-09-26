"""Webhook da Evolution API e roteamento da máquina de estados.

View assíncrona que recebe os webhooks POST da Evolution API,
identifica a sessão do cliente e roteia para o handler correto
com base no estado atual da sessão.
"""

import asyncio
import json
import logging

from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import SessaoChat
from .llm_service import processar_mensagem, LLMResult, MENU_TABACARIA
from .notificacoes import enviar_whatsapp, notificar_teams
from .scraper import cotar_frete_uber

logger = logging.getLogger(__name__)


def _extrair_dados_webhook(body: dict) -> tuple[str, str] | None:
    """Extrai telefone e texto da mensagem do payload da Evolution API.

    Returns:
        Tupla (telefone, texto) ou None se não for mensagem de texto.
    """
    try:
        # Evolution API v2 payload structure
        data = body.get('data', body)
        key = data.get('key', {})
        telefone_raw = key.get('remoteJid', '')
        # Remove @s.whatsapp.net
        telefone = telefone_raw.split('@')[0] if '@' in telefone_raw else telefone_raw

        # Ignora mensagens enviadas pelo próprio bot
        if key.get('fromMe', False):
            return None

        message = data.get('message', {})
        texto = (
            message.get('conversation')
            or message.get('extendedTextMessage', {}).get('text')
            or ''
        )

        if not telefone or not texto.strip():
            return None

        return telefone, texto.strip()
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


async def _handler_atendimento_llm(
    sessao: SessaoChat, texto: str,
) -> None:
    """Processa mensagem no estado de atendimento via LLM."""
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

    elif resultado.tipo == 'fechar_pedido':
        sessao.carrinho_temporario = resultado.dados
        sessao.estado_atual = SessaoChat.Estado.AGUARDANDO_ENDERECO
        await sessao.asave(update_fields=[
            'historico_mensagens', 'ultima_interacao',
            'carrinho_temporario', 'estado_atual',
        ])

        # Monta resumo do carrinho
        itens = resultado.dados.get('itens', [])
        total = sum(i['preco_unitario'] * i['quantidade'] for i in itens)
        resumo_linhas = [f"  • {i['quantidade']}x {i['nome']} — R$ {i['preco_unitario'] * i['quantidade']:.2f}" for i in itens]
        resumo = '\n'.join(resumo_linhas)

        await enviar_whatsapp(
            sessao.telefone,
            f"✅ Pedido confirmado!\n\n"
            f"{resumo}\n\n"
            f"💰 Subtotal: R$ {total:.2f}\n\n"
            f"📍 Agora preciso do seu endereço completo para calcular o frete.\n"
            f"(Rua, número, bairro, cidade)",
        )

    elif resultado.tipo == 'solicitar_humano':
        sessao.estado_atual = SessaoChat.Estado.ATENDIMENTO_HUMANO
        await sessao.asave(update_fields=[
            'historico_mensagens', 'ultima_interacao', 'estado_atual',
        ])
        await enviar_whatsapp(
            sessao.telefone,
            '🔄 Transferindo para um atendente humano... '
            'Aguarde um momento, por favor!',
        )
        motivo = resultado.dados.get('motivo', 'Não especificado')
        await notificar_teams(
            f'🚨 *Transferência para humano*\n'
            f'📱 Cliente: {sessao.telefone}\n'
            f'📝 Motivo: {motivo}\n'
            f'🕐 {timezone.now().strftime("%d/%m/%Y %H:%M")}',
        )


async def _handler_aguardando_endereco(
    sessao: SessaoChat, texto: str,
) -> None:
    """Recebe endereço do cliente e dispara cotação de frete em background."""
    sessao.estado_atual = SessaoChat.Estado.CALCULANDO_FRETE
    sessao.ultima_interacao = timezone.now()
    await sessao.asave(update_fields=['estado_atual', 'ultima_interacao'])

    await enviar_whatsapp(
        sessao.telefone,
        '🔍 Calculando o frete para o seu endereço... Aguarde um momento!',
    )

    # Dispara scraping em background para não bloquear o webhook
    asyncio.create_task(
        _task_calcular_frete(sessao.telefone, texto),
        name=f'frete_{sessao.telefone}',
    )


async def _task_calcular_frete(telefone: str, endereco: str) -> None:
    """Task de background: cota frete, soma totais e envia PIX."""
    try:
        # Busca sessão atualizada
        sessao = await SessaoChat.objects.aget(telefone=telefone)

        # Cota frete via Uber
        resultado_frete = await cotar_frete_uber(endereco)

        if not resultado_frete['sucesso']:
            # Frete falhou — escala para humano
            sessao.estado_atual = SessaoChat.Estado.ATENDIMENTO_HUMANO
            await sessao.asave(update_fields=['estado_atual'])
            await enviar_whatsapp(
                telefone,
                '😕 Não consegui calcular o frete automaticamente. '
                'Um atendente irá ajudá-lo em instantes!',
            )
            await notificar_teams(
                f'⚠️ *Falha na cotação de frete*\n'
                f'📱 Cliente: {telefone}\n'
                f'📍 Endereço: {endereco}\n'
                f'❌ Erro: {resultado_frete["mensagem"]}',
            )
            return

        # Calcula total
        valor_frete = resultado_frete['valor_frete']
        itens = sessao.carrinho_temporario.get('itens', [])
        subtotal = sum(i['preco_unitario'] * i['quantidade'] for i in itens)
        total = subtotal + valor_frete

        # Atualiza sessão
        sessao.estado_atual = SessaoChat.Estado.AGUARDANDO_COMPROVANTE
        sessao.carrinho_temporario['frete'] = valor_frete
        sessao.carrinho_temporario['total'] = total
        sessao.carrinho_temporario['endereco'] = endereco
        sessao.ultima_interacao = timezone.now()
        await sessao.asave(update_fields=[
            'estado_atual', 'carrinho_temporario', 'ultima_interacao',
        ])

        # Monta mensagem de cobrança
        resumo_linhas = [
            f"  • {i['quantidade']}x {i['nome']} — "
            f"R$ {i['preco_unitario'] * i['quantidade']:.2f}"
            for i in itens
        ]
        resumo = '\n'.join(resumo_linhas)

        mensagem_cobranca = (
            f"🧾 *Resumo do Pedido*\n\n"
            f"{resumo}\n\n"
            f"🚚 Frete ({resultado_frete['mensagem']}): R$ {valor_frete:.2f}\n"
            f"💰 *TOTAL: R$ {total:.2f}*\n\n"
            f"────────────────────\n"
            f"💳 *Pagamento via PIX*\n\n"
            f"Chave PIX: `{settings.CHAVE_PIX}`\n\n"
            f"Após o pagamento, envie o comprovante aqui neste chat. ✅"
        )

        await enviar_whatsapp(telefone, mensagem_cobranca)

        # Notifica equipe no Teams
        await notificar_teams(
            f'🛒 *Novo pedido aguardando pagamento*\n'
            f'📱 Cliente: {telefone}\n'
            f'📍 Endereço: {endereco}\n'
            f'💰 Total: R$ {total:.2f}\n'
            f'🚚 Frete: R$ {valor_frete:.2f}',
        )

    except Exception:
        logger.exception('Erro na task de cálculo de frete para %s', telefone)
        try:
            await enviar_whatsapp(
                telefone,
                '😕 Ocorreu um erro. Um atendente irá ajudá-lo em breve!',
            )
        except Exception:
            logger.exception('Falha ao enviar mensagem de erro')


async def _handler_aguardando_comprovante(
    sessao: SessaoChat, texto: str,
) -> None:
    """Lembra o cliente de enviar o comprovante."""
    await enviar_whatsapp(
        sessao.telefone,
        '⏳ Ainda estamos aguardando o comprovante de pagamento PIX.\n'
        f'Chave PIX: `{settings.CHAVE_PIX}`\n\n'
        'Envie a foto ou PDF do comprovante aqui. '
        'Se precisar de ajuda, digite "atendente".',
    )


async def _handler_atendimento_humano(
    sessao: SessaoChat, texto: str,
) -> None:
    """Notifica que a conversa está com um humano."""
    await notificar_teams(
        f'💬 *Mensagem do cliente (atendimento humano)*\n'
        f'📱 {sessao.telefone}: {texto}',
    )


# ── Mapa de handlers por estado ──
_HANDLERS = {
    SessaoChat.Estado.ATENDIMENTO_LLM: _handler_atendimento_llm,
    SessaoChat.Estado.AGUARDANDO_ENDERECO: _handler_aguardando_endereco,
    SessaoChat.Estado.CALCULANDO_FRETE: lambda s, t: enviar_whatsapp(
        s.telefone, '⏳ Ainda estou calculando o frete... Aguarde!',
    ),
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

    telefone, texto = dados
    logger.info('Mensagem recebida de %s: %s', telefone, texto[:100])

    # Obtém ou cria sessão
    sessao = await _obter_ou_criar_sessao(telefone)

    # Roteia para o handler do estado atual
    handler = _HANDLERS.get(sessao.estado_atual)
    if handler:
        # Para o estado calculando_frete, não precisa await do lambda
        if sessao.estado_atual == SessaoChat.Estado.CALCULANDO_FRETE:
            await handler(sessao, texto)
        else:
            await handler(sessao, texto)
    else:
        logger.warning('Estado desconhecido: %s', sessao.estado_atual)

    # Retorna 200 imediato (obrigatório para webhooks)
    return JsonResponse({'status': 'ok'})
