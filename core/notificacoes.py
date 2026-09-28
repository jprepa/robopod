"""Serviço de notificações externas (Evolution API e Microsoft Teams).

Todas as chamadas à Evolution API usam retry automático (até 3 tentativas
com backoff exponencial) e timeout de 30 s para evitar que o bot fique mudo
quando a Evolution demora a responder.
"""

import asyncio
import logging

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

# Timeout generoso para a Evolution API — a instância pode estar ocupada
# processando mídia ou reconectando ao WhatsApp.
_EVO_TIMEOUT = 30

# Configuração de retry
_MAX_TENTATIVAS = 3
_BACKOFF_BASE = 1.5  # segundos — cresce exponencialmente (1.5, 3, 6)


async def _post_com_retry(
    url: str,
    *,
    json: dict,
    headers: dict,
    timeout: int = _EVO_TIMEOUT,
    max_tentativas: int = _MAX_TENTATIVAS,
    label: str = '',
) -> httpx.Response:
    """POST com retry automático e backoff exponencial.

    Raises:
        httpx.HTTPError: Se todas as tentativas falharem.
    """
    ultima_exc: Exception | None = None
    for tentativa in range(1, max_tentativas + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=json, headers=headers)
                resp.raise_for_status()
                return resp
        except Exception as exc:
            ultima_exc = exc
            if tentativa < max_tentativas:
                espera = _BACKOFF_BASE * (2 ** (tentativa - 1))
                logger.warning(
                    '%s — tentativa %d/%d falhou (%s), retentando em %.1fs',
                    label, tentativa, max_tentativas, exc, espera,
                )
                await asyncio.sleep(espera)
            else:
                logger.error(
                    '%s — todas as %d tentativas falharam: %s',
                    label, max_tentativas, exc,
                )
    raise ultima_exc  # type: ignore[misc]


def _evo_headers() -> dict:
    return {
        'Content-Type': 'application/json',
        'apikey': settings.EVOLUTION_API_KEY,
    }


def _evo_url(endpoint: str) -> str:
    return (
        f"{settings.EVOLUTION_API_URL}/message/{endpoint}/"
        f"{settings.EVOLUTION_INSTANCE}"
    )


async def enviar_whatsapp(telefone: str, mensagem: str) -> bool:
    """Envia mensagem de texto via Evolution API.

    Args:
        telefone: Número no formato 55XXXXXXXXXXX.
        mensagem: Texto da mensagem.

    Returns:
        True se o envio foi bem-sucedido.
    """
    try:
        await _post_com_retry(
            _evo_url('sendText'),
            json={'number': telefone, 'text': mensagem},
            headers=_evo_headers(),
            label=f'WhatsApp texto → {telefone}',
        )
        logger.info('WhatsApp enviado para %s', telefone)
        return True
    except Exception:
        logger.exception('Falha ao enviar WhatsApp para %s', telefone)
        return False


async def enviar_imagem_whatsapp(
    telefone: str,
    url_imagem: str,
    legenda: str = '',
) -> bool:
    """Envia imagem com legenda opcional via Evolution API.

    Args:
        telefone: Número no formato 55XXXXXXXXXXX.
        url_imagem: URL pública da imagem.
        legenda: Legenda opcional para a imagem.

    Returns:
        True se o envio foi bem-sucedido.
    """
    try:
        await _post_com_retry(
            _evo_url('sendMedia'),
            json={
                'number': telefone,
                'mediatype': 'image',
                'mimetype': 'image/jpeg',
                'caption': legenda,
                'media': url_imagem,
            },
            headers=_evo_headers(),
            label=f'WhatsApp imagem → {telefone}',
        )
        logger.info('Imagem WhatsApp enviada para %s', telefone)
        return True
    except Exception:
        logger.exception('Falha ao enviar imagem WhatsApp para %s', telefone)
        return False


async def enviar_lista_whatsapp(
    telefone: str,
    titulo: str,
    descricao: str,
    botao_texto: str,
    secoes: list[dict],
    rodape: str = '',
) -> bool:
    """Envia mensagem interativa do tipo lista via Evolution API.

    Args:
        telefone: Número no formato 55XXXXXXXXXXX.
        titulo: Título da mensagem.
        descricao: Descrição/corpo da mensagem.
        botao_texto: Texto do botão principal para abrir a lista.
        secoes: Lista de seções contendo título e opções ('title' e 'rows').
        rodape: Texto de rodapé opcional.

    Returns:
        True se o envio foi bem-sucedido.
    """
    try:
        await _post_com_retry(
            _evo_url('sendList'),
            json={
                'number': telefone,
                'title': titulo,
                'description': descricao,
                'buttonText': botao_texto,
                'footerText': rodape,
                'sections': secoes,
            },
            headers=_evo_headers(),
            label=f'WhatsApp lista → {telefone}',
        )
        logger.info('Lista interativa WhatsApp enviada para %s', telefone)
        return True
    except Exception:
        logger.exception('Falha ao enviar lista WhatsApp para %s', telefone)
        return False


async def enviar_botoes_whatsapp(
    telefone: str,
    titulo: str,
    descricao: str,
    botoes: list[dict],
    rodape: str = '',
) -> bool:
    """Envia mensagem interativa com botões de resposta via Evolution API.

    Args:
        telefone: Número no formato 55XXXXXXXXXXX.
        titulo: Título da mensagem.
        descricao: Descrição/corpo da mensagem.
        botoes: Lista de botões com 'type', 'displayText' e 'id'.
        rodape: Texto de rodapé opcional.

    Returns:
        True se o envio foi bem-sucedido.
    """
    try:
        await _post_com_retry(
            _evo_url('sendButtons'),
            json={
                'number': telefone,
                'title': titulo,
                'description': descricao,
                'footer': rodape,
                'buttons': botoes,
            },
            headers=_evo_headers(),
            label=f'WhatsApp botões → {telefone}',
        )
        logger.info('Botões WhatsApp enviados para %s', telefone)
        return True
    except Exception:
        logger.exception('Falha ao enviar botões WhatsApp para %s', telefone)
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

    try:
        await _post_com_retry(
            webhook_url,
            json={'text': mensagem},
            headers={'Content-Type': 'application/json'},
            timeout=10,
            label='Teams webhook',
        )
        logger.info('Notificação Teams enviada')
        return True
    except Exception:
        logger.exception('Falha ao notificar Teams')
        return False
