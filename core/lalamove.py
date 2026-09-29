"""Cliente da API v3 da Lalamove (cotação de entrega de moto).

Docs: https://developers.lalamove.com/

Autenticação por HMAC-SHA256 sobre "<timestamp>\\r\\n<MÉTODO>\\r\\n<path>\\r\\n\\r\\n<body>".
Cotar é gratuito; a cotação vale 5 minutos e não gera pedido.
"""

import hashlib
import hmac
import json
import logging
import time
import uuid

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

_URL_SANDBOX = 'https://rest.sandbox.lalamove.com'
_URL_PRODUCAO = 'https://rest.lalamove.com'
_MERCADO = 'BR'
_IDIOMA = 'pt_BR'


class LalamoveErro(Exception):
    """Erro retornado pela API da Lalamove (credencial, endereço, saldo etc.)."""


def _base_url() -> str:
    return _URL_SANDBOX if settings.LALAMOVE_SANDBOX else _URL_PRODUCAO


def _headers(metodo: str, path: str, body: str) -> dict:
    timestamp = str(int(time.time() * 1000))
    assinatura = hmac.new(
        settings.LALAMOVE_API_SECRET.encode(),
        f'{timestamp}\r\n{metodo}\r\n{path}\r\n\r\n{body}'.encode(),
        hashlib.sha256,
    ).hexdigest()
    return {
        'Authorization': f'hmac {settings.LALAMOVE_API_KEY}:{timestamp}:{assinatura}',
        'Content-Type': 'application/json',
        'Market': _MERCADO,
        'Request-ID': str(uuid.uuid4()),
    }


async def _requisicao(
    client: httpx.AsyncClient, metodo: str, path: str, dados: dict | None = None,
) -> dict | list:
    if not (settings.LALAMOVE_API_KEY and settings.LALAMOVE_API_SECRET):
        raise LalamoveErro('LALAMOVE_API_KEY/LALAMOVE_API_SECRET não configuradas.')

    # O corpo assinado precisa ser byte a byte o corpo enviado
    body = json.dumps({'data': dados}, ensure_ascii=False) if dados is not None else ''
    resp = await client.request(
        metodo,
        _base_url() + path,
        content=body.encode() if body else None,
        headers=_headers(metodo, path, body),
    )
    if resp.is_error:
        try:
            erros = resp.json().get('errors') or []
            detalhe = '; '.join(
                f"{e.get('id')}: {e.get('detail') or e.get('message')}" for e in erros
            )
        except ValueError:
            detalhe = resp.text[:200]
        raise LalamoveErro(f'HTTP {resp.status_code} — {detalhe or "sem detalhes"}')
    return resp.json().get('data') or {}


async def cotar(
    client: httpx.AsyncClient,
    origem: tuple[str, tuple[float, float]],
    destino: tuple[str, tuple[float, float]],
) -> dict:
    """Cota uma entrega origem -> destino.

    Args:
        origem, destino: (endereço em texto, (lat, lng)).

    Returns:
        dict com valor (float, R$), distancia_km (float), cotacao_id (str)
        e expira_em (str ISO).
    """
    dados = {
        'serviceType': settings.LALAMOVE_SERVICE_TYPE,
        'language': _IDIOMA,
        'stops': [
            {
                'coordinates': {'lat': f'{lat:.7f}', 'lng': f'{lng:.7f}'},
                'address': endereco,
            }
            for endereco, (lat, lng) in (origem, destino)
        ],
    }
    resposta = await _requisicao(client, 'POST', '/v3/quotations', dados)

    preco = resposta.get('priceBreakdown') or {}
    if preco.get('currency') not in (None, 'BRL'):
        raise LalamoveErro(f"Moeda inesperada na cotação: {preco.get('currency')}")
    try:
        valor = float(preco['total'])
    except (KeyError, TypeError, ValueError) as e:
        raise LalamoveErro(f'Cotação sem valor total: {resposta}') from e

    distancia = resposta.get('distance') or {}
    distancia_km = float(distancia.get('value') or 0)
    if distancia.get('unit', 'm') == 'm':
        distancia_km /= 1000

    return {
        'valor': valor,
        'distancia_km': distancia_km,
        'cotacao_id': resposta.get('quotationId', ''),
        'expira_em': resposta.get('expiresAt', ''),
    }


async def listar_servicos(client: httpx.AsyncClient) -> list[dict]:
    """Lista cidades do mercado BR e os serviceType disponíveis em cada uma."""
    cidades = await _requisicao(client, 'GET', '/v3/cities')
    return [
        {
            'cidade': c.get('name'),
            'locode': c.get('locode'),
            'servicos': [s.get('key') for s in c.get('services') or []],
        }
        for c in cidades
    ]
