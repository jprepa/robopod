"""Envio de mensagens pelo WhatsApp (Evolution API) e avisos à equipe num grupo.

Cada loja é uma instância da Evolution: a mensagem ao cliente sai pelo número
por onde ele falou por último (painel/lojas.py); avisos ao grupo e a lista de
grupos usam a loja principal.

Todas as chamadas à Evolution API usam retry automático (até 3 tentativas
com backoff exponencial) e timeout de 30 s para evitar que o bot fique mudo
quando a Evolution demora a responder.
"""

import asyncio
import base64
import logging
import random
import time

import httpx
from django.conf import settings

from painel.lojas import instancia_principal, rota
from painel.models import Configuracao, Loja
from painel.servicos import registrar_mensagem_enviada

logger = logging.getLogger(__name__)

# Timeout generoso para a Evolution API — a instância pode estar ocupada
# processando mídia ou reconectando ao WhatsApp.
_EVO_TIMEOUT = 30

# Configuração de retry
_MAX_TENTATIVAS = 3
_BACKOFF_BASE = 1.5  # segundos — cresce exponencialmente (1.5, 3, 6)

# "Digitando..." antes de cada resposta do bot, para não parecer robô.
# Cresce com o tamanho do texto e varia um pouco a cada mensagem: tempo
# sempre igual para o mesmo texto é a marca mais fácil de um robô.
_DIGITANDO_MIN_MS = 1200
_DIGITANDO_MS_POR_CARACTERE = 30
_DIGITANDO_MAX_MS = 6000
_DIGITANDO_VARIACAO = 0.25  # ±25%

# Duas mensagens do bot seguidas (boas-vindas + cardápio, vários sabores):
# uma pausa entre elas, como quem termina uma e começa a próxima.
_EM_SEQUENCIA_S = 6
_PAUSA_ENTRE_MENSAGENS_S = (0.8, 2.0)
_ultimo_envio: dict[str, float] = {}


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


def _evo_url(endpoint: str, instancia: str) -> str:
    return f"{settings.EVOLUTION_API_URL}/message/{endpoint}/{instancia}"


def _delay_digitando(texto: str) -> int:
    """Milissegundos de "digitando..." que a Evolution mostra antes de enviar."""
    ms = min(_DIGITANDO_MIN_MS + len(texto) * _DIGITANDO_MS_POR_CARACTERE, _DIGITANDO_MAX_MS)
    return int(ms * random.uniform(1 - _DIGITANDO_VARIACAO, 1 + _DIGITANDO_VARIACAO))


async def _pausa_se_em_sequencia(telefone: str) -> None:
    """Espera um pouco se o bot acabou de mandar outra mensagem a esse cliente."""
    ultimo = _ultimo_envio.get(telefone)
    if ultimo is not None and time.monotonic() - ultimo < _EM_SEQUENCIA_S:
        await asyncio.sleep(random.uniform(*_PAUSA_ENTRE_MENSAGENS_S))


def _registrar_envio(telefone: str) -> None:
    agora = time.monotonic()
    _ultimo_envio[telefone] = agora
    if len(_ultimo_envio) > 5000:  # esquece quem não recebe nada há tempo
        for numero, quando in list(_ultimo_envio.items()):
            if agora - quando > _EM_SEQUENCIA_S:
                del _ultimo_envio[numero]


async def marcar_como_lidas(instancia: str, chaves: list[dict]) -> None:
    """Marca as mensagens do cliente como lidas (tracinhos azuis), como quem
    abre a conversa antes de responder. Falha não impede a resposta."""
    chaves = [c for c in chaves if c.get('id') and c.get('remoteJid')]
    if not chaves:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{settings.EVOLUTION_API_URL}/chat/markMessageAsRead/{instancia}",
                json={'readMessages': [
                    {'remoteJid': c['remoteJid'], 'fromMe': False, 'id': c['id']} for c in chaves
                ]},
                headers=_evo_headers(),
            )
        resp.raise_for_status()
    except Exception as e:
        logger.warning('Não foi possível marcar como lidas as mensagens (%s): %s', instancia, e)


async def enviar_whatsapp(telefone: str, mensagem: str, autor: str = 'bot') -> bool:
    """Envia mensagem de texto via Evolution API e registra no histórico.

    Args:
        telefone: Número no formato 55XXXXXXXXXXX.
        mensagem: Texto da mensagem.
        autor: 'bot' ou 'atendente' (quem aparece como remetente no painel).

    Returns:
        True se o envio foi bem-sucedido.
    """
    payload = {'number': telefone, 'text': mensagem}
    if autor == 'bot':
        # Mensagem de atendente já foi digitada por uma pessoa, não precisa
        payload['delay'] = _delay_digitando(mensagem)
        await _pausa_se_em_sequencia(telefone)
    instancia, loja_id = await rota(telefone)
    try:
        await _post_com_retry(
            _evo_url('sendText', instancia),
            json=payload,
            headers=_evo_headers(),
            label=f'WhatsApp texto → {telefone}',
        )
        _registrar_envio(telefone)
        logger.info('WhatsApp enviado para %s', telefone)
        enviado = True
    except Exception:
        logger.exception('Falha ao enviar WhatsApp para %s', telefone)
        enviado = False
    await registrar_mensagem_enviada(telefone, mensagem, autor, enviado, loja_id)
    return enviado


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
    await _pausa_se_em_sequencia(telefone)
    instancia, loja_id = await rota(telefone)
    try:
        await _post_com_retry(
            _evo_url('sendMedia', instancia),
            json={
                'number': telefone,
                'mediatype': 'image',
                'mimetype': 'image/jpeg',
                'caption': legenda,
                'media': url_imagem,
                'delay': _delay_digitando(legenda),
            },
            headers=_evo_headers(),
            label=f'WhatsApp imagem → {telefone}',
        )
        _registrar_envio(telefone)
        logger.info('Imagem WhatsApp enviada para %s', telefone)
        enviado = True
    except Exception:
        logger.exception('Falha ao enviar imagem WhatsApp para %s', telefone)
        enviado = False
    await registrar_mensagem_enviada(
        telefone, f'[imagem] {legenda}'.strip(), 'bot', enviado, loja_id,
    )
    return enviado


async def enviar_documento_whatsapp(
    telefone: str,
    conteudo: bytes,
    nome_arquivo: str,
    legenda: str = '',
    mimetype: str = 'application/pdf',
) -> bool:
    """Envia um arquivo (ex.: PDF do cardápio) via Evolution API.

    O arquivo vai em base64 no corpo, então não precisa de URL pública.

    Returns:
        True se o envio foi bem-sucedido.
    """
    await _pausa_se_em_sequencia(telefone)
    instancia, loja_id = await rota(telefone)
    try:
        await _post_com_retry(
            _evo_url('sendMedia', instancia),
            json={
                'number': telefone,
                'mediatype': 'document',
                'mimetype': mimetype,
                'fileName': nome_arquivo,
                'caption': legenda,
                'media': base64.b64encode(conteudo).decode(),
                'delay': _delay_digitando(legenda),
            },
            headers=_evo_headers(),
            timeout=90,  # arquivo grande: a Evolution ainda sobe para o WhatsApp
            label=f'WhatsApp documento → {telefone}',
        )
        _registrar_envio(telefone)
        logger.info('Documento WhatsApp enviado para %s', telefone)
        enviado = True
    except Exception:
        logger.exception('Falha ao enviar documento WhatsApp para %s', telefone)
        enviado = False
    await registrar_mensagem_enviada(
        telefone, f'[documento] {nome_arquivo} {legenda}'.strip(), 'bot', enviado, loja_id,
    )
    return enviado


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
    await _pausa_se_em_sequencia(telefone)
    instancia, loja_id = await rota(telefone)
    try:
        await _post_com_retry(
            _evo_url('sendList', instancia),
            json={
                'number': telefone,
                'title': titulo,
                'description': descricao,
                'buttonText': botao_texto,
                'footerText': rodape,
                'sections': secoes,
                'delay': _delay_digitando(descricao),
            },
            headers=_evo_headers(),
            label=f'WhatsApp lista → {telefone}',
        )
        _registrar_envio(telefone)
        logger.info('Lista interativa WhatsApp enviada para %s', telefone)
        enviado = True
    except Exception:
        logger.exception('Falha ao enviar lista WhatsApp para %s', telefone)
        enviado = False
    await registrar_mensagem_enviada(telefone, f'[lista] {titulo}', 'bot', enviado, loja_id)
    return enviado


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
    await _pausa_se_em_sequencia(telefone)
    instancia, loja_id = await rota(telefone)
    try:
        await _post_com_retry(
            _evo_url('sendButtons', instancia),
            json={
                'number': telefone,
                'title': titulo,
                'description': descricao,
                'footer': rodape,
                'buttons': botoes,
                'delay': _delay_digitando(descricao),
            },
            headers=_evo_headers(),
            label=f'WhatsApp botões → {telefone}',
        )
        _registrar_envio(telefone)
        logger.info('Botões WhatsApp enviados para %s', telefone)
        enviado = True
    except Exception:
        logger.exception('Falha ao enviar botões WhatsApp para %s', telefone)
        enviado = False
    await registrar_mensagem_enviada(telefone, f'[botões] {titulo}', 'bot', enviado, loja_id)
    return enviado


async def notificar_equipe(
    mensagem: str, telefone: str = '', mensagem_de_cliente: bool = False,
) -> bool:
    """Avisa a equipe no grupo do WhatsApp escolhido em Painel > Configurações.

    Args:
        mensagem: Texto do aviso (aceita *negrito* do WhatsApp).
        telefone: Cliente do aviso; com PAINEL_URL definido, vira um link
            direto para a conversa no painel.
        mensagem_de_cliente: True para o repasse de mensagens em atendimento
            humano, que pode ser desligado separadamente.

    Returns:
        True se o aviso foi enviado.
    """
    config = await Configuracao.aobter()
    if not (config.avisos_ativos and config.grupo_avisos_id):
        logger.info('Grupo de avisos não configurado, aviso ignorado.')
        return False
    if mensagem_de_cliente and not config.avisar_mensagens_atendimento:
        return False

    if telefone and await Loja.objects.filter(ativa=True).acount() > 1:
        loja = await Loja.objects.filter(clientes__telefone=telefone).afirst()
        if loja is not None:
            mensagem += f'\n📱 Pelo número: {loja.nome}'
    if telefone and settings.PAINEL_URL:
        mensagem += f"\n🔗 {settings.PAINEL_URL.rstrip('/')}/painel/conversas/{telefone}/"
    try:
        await _post_com_retry(
            _evo_url('sendText', await instancia_principal()),
            json={'number': config.grupo_avisos_id, 'text': mensagem},
            headers=_evo_headers(),
            label='Aviso no grupo da equipe',
        )
        logger.info('Aviso enviado ao grupo %s', config.grupo_avisos_nome or config.grupo_avisos_id)
        return True
    except Exception:
        logger.exception('Falha ao avisar o grupo da equipe')
        return False


async def listar_grupos_whatsapp(timeout: float = 10) -> list[dict]:
    """Grupos dos quais o WhatsApp da loja participa: [{'id', 'nome', 'participantes'}].

    Raises:
        httpx.HTTPError: se a Evolution não responder.
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(
            f"{settings.EVOLUTION_API_URL}/group/fetchAllGroups/{await instancia_principal()}",
            params={'getParticipants': 'false'},
            headers=_evo_headers(),
        )
        resp.raise_for_status()
    grupos = [
        {
            'id': g.get('id', ''),
            'nome': g.get('subject') or g.get('id', ''),
            'participantes': g.get('size') or len(g.get('participants') or []),
        }
        for g in resp.json() or []
        if str(g.get('id', '')).endswith('@g.us')
    ]
    return sorted(grupos, key=lambda g: g['nome'].lower())


# ── Números (instâncias da Evolution), usados na página Lojas do painel ──


async def estado_conexao(instancia: str, timeout: float = 5) -> str:
    """'open' (conectado), 'connecting', 'close' ou '' se a Evolution não respondeu."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"{settings.EVOLUTION_API_URL}/instance/connectionState/{instancia}",
                headers=_evo_headers(),
            )
        if resp.status_code == 404:
            return 'inexistente'
        resp.raise_for_status()
        return ((resp.json() or {}).get('instance') or {}).get('state') or ''
    except Exception as e:
        logger.warning('Estado da instância %s indisponível: %s', instancia, e)
        return ''


async def criar_instancia(instancia: str) -> None:
    """Cria a instância na Evolution; se ela já existe, não faz nada.

    Raises:
        httpx.HTTPError: se a Evolution recusar ou não responder.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{settings.EVOLUTION_API_URL}/instance/create",
            json={'instanceName': instancia, 'integration': 'WHATSAPP-BAILEYS', 'qrcode': True},
            headers=_evo_headers(),
        )
    if resp.status_code in (400, 403, 409) and 'already in use' in resp.text:
        return
    resp.raise_for_status()


async def qr_code(instancia: str) -> str:
    """Imagem (data:image/png;base64,...) do QR Code para conectar o número, ou ''."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{settings.EVOLUTION_API_URL}/instance/connect/{instancia}",
                headers=_evo_headers(),
            )
        resp.raise_for_status()
        imagem = (resp.json() or {}).get('base64') or ''
    except Exception as e:
        logger.warning('QR Code da instância %s indisponível: %s', instancia, e)
        return ''
    if imagem and not imagem.startswith('data:'):
        imagem = f'data:image/png;base64,{imagem}'
    return imagem


async def baixar_midia(data: dict, instancia: str) -> tuple[bytes, str] | None:
    """Conteúdo e mimetype de uma mídia recebida (áudio ou imagem), ou None.

    Usa o base64 que já vem no webhook (opção "webhook_base64" da Evolution);
    sem ele, pede o arquivo à instância da Evolution que recebeu a mensagem.
    """
    mensagem = data.get('message') or {}
    documento = mensagem.get('documentMessage') or (
        (mensagem.get('documentWithCaptionMessage') or {}).get('message') or {}
    ).get('documentMessage')
    if 'imageMessage' in mensagem:
        midia, padrao = mensagem.get('imageMessage') or {}, 'image/jpeg'
    elif documento is not None:
        midia, padrao = documento, 'application/pdf'
    else:
        midia, padrao = mensagem.get('audioMessage') or {}, 'audio/ogg'
    mimetype = (midia.get('mimetype') or padrao).split(';')[0].strip()
    if mensagem.get('base64'):
        try:
            return base64.b64decode(mensagem['base64']), mimetype
        except ValueError:
            logger.warning('base64 inválido no webhook, buscando a mídia na Evolution')
    try:
        resp = await _post_com_retry(
            f"{settings.EVOLUTION_API_URL}/chat/getBase64FromMediaMessage/{instancia}",
            json={'message': {'key': data.get('key') or {}}, 'convertToMp4': False},
            headers=_evo_headers(),
            timeout=60,
            label='Baixar mídia da Evolution',
        )
        corpo = resp.json() or {}
        conteudo = base64.b64decode(corpo.get('base64') or '')
    except Exception:
        logger.exception('Falha ao baixar a mídia da mensagem')
        return None
    if not conteudo:
        return None
    return conteudo, (corpo.get('mimetype') or mimetype).split(';')[0].strip()
