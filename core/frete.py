"""Cotação de frete para a entrega (moto).

O site da Uber (m.uber.com/go/delivery) exige login, então o scraping sem
sessão nunca chega à tela de preços. Por padrão o frete é ESTIMADO pela
distância de rota loja -> cliente, aplicando a tabela configurada no .env:

    frete = max(FRETE_VALOR_MINIMO, FRETE_TAXA_BASE + km * FRETE_VALOR_KM)
    frete = frete * (1 + FRETE_MARGEM_PERCENTUAL / 100), arredondado p/ cima em R$ 0,50

Provedores de distância:
    - Google Routes API, se GOOGLE_MAPS_API_KEY estiver definida (mais preciso
      para endereços brasileiros);
    - OpenStreetMap (Nominatim + OSRM) gratuito, caso contrário.

FRETE_MODO=uber_scraper mantém o scraper antigo (core/scraper.py).
"""

import logging
import math
import re

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

_USER_AGENT = 'RobopodDeliveryBot/1.0'
_NOMINATIM_URL = 'https://nominatim.openstreetmap.org/search'
_OSRM_URL = 'https://router.project-osrm.org/route/v1/driving'
_GOOGLE_ROUTES_URL = 'https://routes.googleapis.com/directions/v2:computeRoutes'

# Fator rota/linha reta usado quando o roteamento falha mas a geocodificação não
_FATOR_ROTA = 1.35

# Coordenadas da loja (geocodificadas uma vez por processo)
_coords_loja: tuple[float, float] | None = None


class FreteErro(Exception):
    """Falha ao estimar o frete (endereço não encontrado, serviço fora etc.)."""


def _resultado_falha(mensagem: str) -> dict:
    return {'sucesso': False, 'valor_frete': None, 'mensagem': mensagem}


def calcular_valor_frete(distancia_km: float) -> float:
    """Aplica a tabela de preço configurada sobre a distância em km."""
    valor = settings.FRETE_TAXA_BASE + distancia_km * settings.FRETE_VALOR_KM
    valor = max(valor, settings.FRETE_VALOR_MINIMO)
    valor *= 1 + settings.FRETE_MARGEM_PERCENTUAL / 100
    return math.ceil(round(valor, 2) * 2) / 2  # arredonda p/ cima em R$ 0,50


# ── Google Routes API ──


async def _distancia_google(
    client: httpx.AsyncClient, origem: str, destino: str,
) -> float:
    resp = await client.post(
        _GOOGLE_ROUTES_URL,
        headers={
            'X-Goog-Api-Key': settings.GOOGLE_MAPS_API_KEY,
            'X-Goog-FieldMask': 'routes.distanceMeters',
        },
        json={
            'origin': {'address': origem},
            'destination': {'address': destino},
            'travelMode': 'DRIVE',
            'languageCode': 'pt-BR',
            'regionCode': 'br',
        },
    )
    resp.raise_for_status()
    rotas = resp.json().get('routes') or []
    if not rotas or 'distanceMeters' not in rotas[0]:
        raise FreteErro('Endereço não encontrado pelo Google Maps.')
    return rotas[0]['distanceMeters'] / 1000


# ── OpenStreetMap (Nominatim + OSRM) ──


async def _geocodificar_osm(
    client: httpx.AsyncClient,
    endereco: str,
    perto_de: tuple[float, float] | None = None,
) -> tuple[float, float]:
    """Retorna (lat, lon). Tenta o endereço completo e depois sem número/CEP,
    já que muitos números de casa não estão mapeados no OSM."""
    params = {
        'format': 'jsonv2',
        'countrycodes': 'br',
        'limit': 1,
    }
    if perto_de:
        # Prioriza resultados na região da loja (~30 km) sem excluir o resto
        lat, lon = perto_de
        params['viewbox'] = f'{lon - 0.3},{lat + 0.3},{lon + 0.3},{lat - 0.3}'

    sem_numero = re.sub(r'\b\d{5}-?\d{3}\b|\b\d{1,5}\b', '', endereco)
    sem_numero = re.sub(r'\s*,\s*(,\s*)+', ', ', sem_numero).strip(' ,-')

    for consulta in dict.fromkeys([endereco, sem_numero]):  # sem duplicar
        if not consulta:
            continue
        resp = await client.get(_NOMINATIM_URL, params={**params, 'q': consulta})
        resp.raise_for_status()
        resultados = resp.json()
        if resultados:
            return float(resultados[0]['lat']), float(resultados[0]['lon'])

    raise FreteErro(f'Endereço não encontrado no mapa: {endereco}')


def _distancia_linha_reta_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * 6371 * math.asin(math.sqrt(h))


async def _distancia_osm(
    client: httpx.AsyncClient, origem: str, destino: str,
) -> float:
    global _coords_loja
    if _coords_loja is None:
        _coords_loja = await _geocodificar_osm(client, origem)
    coords_cliente = await _geocodificar_osm(client, destino, perto_de=_coords_loja)

    (lat1, lon1), (lat2, lon2) = _coords_loja, coords_cliente
    try:
        resp = await client.get(
            f'{_OSRM_URL}/{lon1},{lat1};{lon2},{lat2}',
            params={'overview': 'false'},
        )
        resp.raise_for_status()
        return resp.json()['routes'][0]['distance'] / 1000
    except Exception:
        logger.warning('OSRM indisponível, usando linha reta x %.2f', _FATOR_ROTA)
        return _distancia_linha_reta_km(_coords_loja, coords_cliente) * _FATOR_ROTA


# ── Ponto de entrada ──


async def estimar_frete(endereco_cliente: str) -> dict:
    """Estima o frete pela distância de rota loja -> cliente.

    Returns:
        dict com sucesso, valor_frete, mensagem e (em caso de sucesso)
        distancia_km.
    """
    origem = settings.ENDERECO_LOJA
    provedor = 'Google' if settings.GOOGLE_MAPS_API_KEY else 'OSM'
    logger.info('Estimando frete (%s): %s -> %s', provedor, origem, endereco_cliente)

    try:
        async with httpx.AsyncClient(
            timeout=15, headers={'User-Agent': _USER_AGENT},
        ) as client:
            if settings.GOOGLE_MAPS_API_KEY:
                distancia_km = await _distancia_google(client, origem, endereco_cliente)
            else:
                distancia_km = await _distancia_osm(client, origem, endereco_cliente)
    except FreteErro as e:
        logger.warning('Frete não estimado: %s', e)
        return _resultado_falha(str(e))
    except Exception:
        logger.exception('Erro ao estimar frete')
        return _resultado_falha('Erro no serviço de mapas ao estimar o frete.')

    if distancia_km > settings.FRETE_DISTANCIA_MAXIMA_KM:
        return _resultado_falha(
            f'Endereço a {distancia_km:.1f} km, acima do limite de '
            f'{settings.FRETE_DISTANCIA_MAXIMA_KM:.0f} km (fora da área ou '
            f'endereço interpretado errado).',
        )

    valor = calcular_valor_frete(distancia_km)
    logger.info('Frete estimado: %.1f km -> R$ %.2f', distancia_km, valor)
    return {
        'sucesso': True,
        'valor_frete': valor,
        'distancia_km': distancia_km,
        'mensagem': f'entrega moto, {distancia_km:.1f} km'.replace('.', ','),
    }


async def cotar_frete(endereco_cliente: str) -> dict:
    """Cota o frete conforme FRETE_MODO ('estimativa' ou 'uber_scraper')."""
    if settings.FRETE_MODO == 'uber_scraper':
        from .scraper import cotar_frete_uber
        return await cotar_frete_uber(endereco_cliente)
    return await estimar_frete(endereco_cliente)
