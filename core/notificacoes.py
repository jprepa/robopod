"""Serviço de notificações externas (Evolution API e Microsoft Teams)."""

import logging

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)


async def enviar_whatsapp(telefone: str, mensagem: str) -> bool:
    """Envia mensagem de texto via Evolution API.

    Args:
        telefone: Número no formato 55XXXXXXXXXXX.
        mensagem: Texto da mensagem.

    Returns:
        True se o envio foi bem-sucedido.
    """
    url = (
        f"{settings.EVOLUTION_API_URL}/message/sendText/"
        f"{settings.EVOLUTION_INSTANCE}"
    )
    headers = {
        'Content-Type': 'application/json',
        'apikey': settings.EVOLUTION_API_KEY,
    }
    payload = {
        'number': telefone,
        'text': mensagem,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            logger.info('WhatsApp enviado para %s', telefone)
            return True
    except Exception:
        logger.exception('Falha ao enviar WhatsApp para %s', telefone)
        return False


async def notificar_teams(mensagem: str) -> bool:
    """Envia notificação para o canal do Microsoft Teams via Incoming Webhook.

    Args:
        mensagem: Texto da notificação.

    Returns:
        True se o envio foi bem-sucedido.
    """
    webhook_url = settings.TEAMS_WEBHOOK_URL
    if not webhook_url:
        logger.warning('TEAMS_WEBHOOK_URL não configurada, notificação ignorada.')
        return False

    payload = {
        'text': mensagem,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(webhook_url, json=payload)
            resp.raise_for_status()
            logger.info('Notificação Teams enviada')
            return True
    except Exception:
        logger.exception('Falha ao notificar Teams')
        return False
