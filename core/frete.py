"""Cotação de frete para a entrega (moto).

Modos (FRETE_MODO no .env):

    - lalamove: cota na API da Lalamove (preço real da entrega de moto). Se a
      API falhar, cai na estimativa abaixo para o cliente não ficar sem resposta.
    - estimativa: distância de rota loja -> cliente aplicada à tabela do .env:

          frete = max(FRETE_VALOR_MINIMO, FRETE_TAXA_BASE + km * FRETE_VALOR_KM)

    - uber_scraper: scraper antigo de m.uber.com (exige login; não funciona na VPS).

Em todos os modos o valor recebe FRETE_MARGEM_PERCENTUAL e é arredondado para
cima em R$ 0,50.

Provedores de mapa (distância e coordenadas):
    - Google (Routes/Geocoding API), se GOOGLE_MAPS_API_KEY estiver definida;
    - OpenStreetMap (Nominatim + OSRM) gratuito, caso contrário ou se o Google falhar.
"""

import logging
import math
import re

import httpx
from django.conf import settings

from . import lalamove

logger = logging.getLogger(__name__)

_USER_AGENT = 'RobopodDeliveryBot/1.0'
_NOMINATIM_URL = 'https://nominatim.openstreetmap.org/search'
_OSRM_URL = 'https://router.project-osrm.org/route/v1/driving'
_GOOGLE_ROUTES_URL = 'https://routes.googleapis.com/directions/v2:computeRoutes'
_GOOGLE_GEOCODE_URL = 'https://maps.googleapis.com/maps/api/geocode/json'

# Fator rota/linha reta usado quando o roteamento falha mas a geocodificação não
_FATOR_ROTA = 1.35

# Coordenadas da loja (LOJA_COORDENADAS ou geocodificadas uma vez por processo)
_coords_loja: tuple[float, float] | None = None


class FreteErro(Exception):
    """Falha ao estimar o frete (endereço não encontrado, serviço fora etc.)."""


def _resultado_falha(mensagem: str) -> dict:
    return {'sucesso': False, 'valor_frete': None, 'mensagem': mensagem}


def _aplicar_margem(valor: float) -> float:
    """Soma a margem configurada e arredonda para cima em R$ 0,50."""
    valor *= 1 + settings.FRETE_MARGEM_PERCENTUAL / 100
    return math.ceil(round(valor, 2) * 2) / 2


def calcular_valor_frete(distancia_km: float) -> float:
    """Aplica a tabela de preço configurada sobre a distância em km."""
    valor = settings.FRETE_TAXA_BASE + distancia_km * settings.FRETE_VALOR_KM
    return _aplicar_margem(max(valor, settings.FRETE_VALOR_MINIMO))


def _falha_distancia(distancia_km: float) -> dict | None:
    if distancia_km <= settings.FRETE_DISTANCIA_MAXIMA_KM:
        return None
    return _resultado_falha(
        f'Endereço a {distancia_km:.1f} km, acima do limite de '
        f'{settings.FRETE_DISTANCIA_MAXIMA_KM:.0f} km (fora da área ou '
        f'endereço interpretado errado).',
    )


def _descricao(distancia_km: float) -> str:
    return f'entrega moto, {distancia_km:.1f} km'.replace('.', ',')


# ── Google Maps ──


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


async def _geocodificar_google(
    client: httpx.AsyncClient, endereco: str,
) -> tuple[float, float]:
    resp = await client.get(_GOOGLE_GEOCODE_URL, params={
        'address': endereco,
        'key': settings.GOOGLE_MAPS_API_KEY,
        'region': 'br',
        'language': 'pt-BR',
    })
    resp.raise_for_status()
    dados = resp.json()
    if dados.get('status') != 'OK' or not dados.get('results'):
        raise FreteErro(f"Google Geocoding: {dados.get('status')}")
    local = dados['results'][0]['geometry']['location']
    return local['lat'], local['lng']


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


# ── Coordenadas (Google com fallback para OSM) ──


async def _geocodificar(
    client: httpx.AsyncClient,
    endereco: str,
    perto_de: tuple[float, float] | None = None,
) -> tuple[float, float]:
    if settings.GOOGLE_MAPS_API_KEY:
        try:
            return await _geocodificar_google(client, endereco)
        except Exception as e:
            logger.warning('Google Geocoding falhou (%s), tentando OSM', e)
    return await _geocodificar_osm(client, endereco, perto_de=perto_de)


async def _obter_coords_loja(client: httpx.AsyncClient) -> tuple[float, float]:
    global _coords_loja
    if _coords_loja is None:
        if settings.LOJA_COORDENADAS:
            lat, lng = (float(v) for v in settings.LOJA_COORDENADAS.split(','))
            _coords_loja = (lat, lng)
        else:
            _coords_loja = await _geocodificar(client, settings.ENDERECO_LOJA)
    return _coords_loja


async def _distancia_osm(
    client: httpx.AsyncClient, destino: str,
) -> float:
    coords_loja = await _obter_coords_loja(client)
    coords_cliente = await _geocodificar_osm(client, destino, perto_de=coords_loja)

    (lat1, lon1), (lat2, lon2) = coords_loja, coords_cliente
    try:
        resp = await client.get(
            f'{_OSRM_URL}/{lon1},{lat1};{lon2},{lat2}',
            params={'overview': 'false'},
        )
        resp.raise_for_status()
        return resp.json()['routes'][0]['distance'] / 1000
    except Exception:
        logger.warning('OSRM indisponível, usando linha reta x %.2f', _FATOR_ROTA)
        return _distancia_linha_reta_km(coords_loja, coords_cliente) * _FATOR_ROTA


def _novo_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=15, headers={'User-Agent': _USER_AGENT})


# ── Estimativa por distância ──


async def estimar_frete(endereco_cliente: str) -> dict:
    """Estima o frete pela distância de rota loja -> cliente.

    Returns:
        dict com sucesso, valor_frete, mensagem e (em caso de sucesso)
        distancia_km e fornecedor.
    """
    origem = settings.ENDERECO_LOJA
    provedor = 'Google' if settings.GOOGLE_MAPS_API_KEY else 'OSM'
    logger.info('Estimando frete (%s): %s -> %s', provedor, origem, endereco_cliente)

    try:
        async with _novo_client() as client:
            if settings.GOOGLE_MAPS_API_KEY:
                distancia_km = await _distancia_google(client, origem, endereco_cliente)
            else:
                distancia_km = await _distancia_osm(client, endereco_cliente)
    except FreteErro as e:
        logger.warning('Frete não estimado: %s', e)
        return _resultado_falha(str(e))
    except Exception:
        logger.exception('Erro ao estimar frete')
        return _resultado_falha('Erro no serviço de mapas ao estimar o frete.')

    if falha := _falha_distancia(distancia_km):
        return falha

    valor = calcular_valor_frete(distancia_km)
    logger.info('Frete estimado: %.1f km -> R$ %.2f', distancia_km, valor)
    return {
        'sucesso': True,
        'valor_frete': valor,
        'distancia_km': distancia_km,
        'fornecedor': 'estimativa',
        'mensagem': _descricao(distancia_km),
    }


# ── Lalamove ──


async def cotar_frete_lalamove(endereco_cliente: str) -> dict:
    """Cota o frete na API da Lalamove (preço real da moto)."""
    logger.info('Cotando Lalamove: %s -> %s', settings.ENDERECO_LOJA, endereco_cliente)
    try:
        async with _novo_client() as client:
            coords_loja = await _obter_coords_loja(client)
            coords_cliente = await _geocodificar(
                client, endereco_cliente, perto_de=coords_loja,
            )
            cotacao = await lalamove.cotar(
                client,
                origem=(settings.ENDERECO_LOJA, coords_loja),
                destino=(endereco_cliente, coords_cliente),
            )
    except (FreteErro, lalamove.LalamoveErro) as e:
        logger.warning('Lalamove não cotou: %s', e)
        return _resultado_falha(str(e))
    except Exception:
        logger.exception('Erro ao cotar na Lalamove')
        return _resultado_falha('Erro ao cotar na Lalamove.')

    distancia_km = cotacao['distancia_km']
    if falha := _falha_distancia(distancia_km):
        return falha

    valor = _aplicar_margem(cotacao['valor'])
    logger.info(
        'Lalamove: %.1f km, R$ %.2f (cobrado R$ %.2f), cotação %s',
        distancia_km, cotacao['valor'], valor, cotacao['cotacao_id'],
    )
    return {
        'sucesso': True,
        'valor_frete': valor,
        'distancia_km': distancia_km,
        'fornecedor': 'lalamove',
        'custo_fornecedor': cotacao['valor'],
        'cotacao_id': cotacao['cotacao_id'],
        'mensagem': _descricao(distancia_km),
    }


# ── Ponto de entrada ──


async def cotar_frete(endereco_cliente: str) -> dict:
    """Cota o frete conforme FRETE_MODO ('lalamove', 'estimativa' ou 'uber_scraper')."""
    if settings.FRETE_MODO == 'lalamove':
        resultado = await cotar_frete_lalamove(endereco_cliente)
        if resultado['sucesso']:
            return resultado
        logger.warning(
            'Lalamove falhou (%s); usando estimativa por distância',
            resultado['mensagem'],
        )
        return await estimar_frete(endereco_cliente)

    if settings.FRETE_MODO == 'uber_scraper':
        from .scraper import cotar_frete_uber
        return await cotar_frete_uber(endereco_cliente)

    return await estimar_frete(endereco_cliente)
