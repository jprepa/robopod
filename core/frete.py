"""Cotação de frete para a entrega (moto).

Modos (FRETE_MODO no .env):

    - lalamove: cota na API da Lalamove (preço real da entrega de moto). Se a
      API falhar, cai na estimativa abaixo para o cliente não ficar sem resposta.
    - estimativa: distância de rota loja -> cliente aplicada à tabela do .env:

          frete = max(FRETE_VALOR_MINIMO, FRETE_TAXA_BASE + km * FRETE_VALOR_KM)

    - uber_scraper: scraper antigo de m.uber.com (exige login; não funciona na VPS).

Em todos os modos o valor recebe FRETE_MARGEM_PERCENTUAL e é arredondado para
cima em R$ 0,50.

O endereço do cliente é localizado UMA vez (com viés para a região da loja e,
se ele não estiver lá, com a cidade da loja anexada) e as mesmas coordenadas
alimentam a Lalamove e a estimativa. Assim o mesmo texto sempre dá o mesmo
ponto e o mesmo frete.

Provedores de mapa (distância e coordenadas):
    - Google (Routes/Geocoding API), se GOOGLE_MAPS_API_KEY estiver definida;
    - OpenStreetMap (Nominatim + OSRM) gratuito, caso contrário ou se o Google falhar.
"""

import asyncio
import logging
import math
import re
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass

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

# Status HTTP que valem nova tentativa (limite de taxa / instabilidade)
_STATUS_RETENTAVEIS = {429, 500, 502, 503, 504}
_TENTATIVAS = 3

# Resultados que são só a cidade/estado/país: o endereço em si não foi achado
_TIPOS_IMPRECISOS_GOOGLE = {
    'country', 'administrative_area_level_1', 'administrative_area_level_2',
    'locality', 'political', 'postal_code',
}
_TIPOS_IMPRECISOS_OSM = {
    'country', 'state', 'region', 'state_district', 'county',
    'municipality', 'city', 'town', 'postcode',
}

# Motivos de falha (o bot decide o que dizer ao cliente a partir deles)
NAO_ENCONTRADO = 'nao_encontrado'
FORA_AREA = 'fora_area'
ERRO_SERVICO = 'erro_servico'


@dataclass(frozen=True)
class Local:
    lat: float
    lng: float
    endereco_formatado: str

    @property
    def coords(self) -> tuple[float, float]:
        return self.lat, self.lng


class FreteErro(Exception):
    """Falha ao estimar o frete (endereço não encontrado, serviço fora etc.)."""

    def __init__(self, mensagem: str, motivo: str = ERRO_SERVICO):
        super().__init__(mensagem)
        self.motivo = motivo


# Coordenadas da loja (LOJA_COORDENADAS ou geocodificadas uma vez por processo)
_loja: Local | None = None

# Cache de endereços já localizados (texto normalizado -> Local)
_CACHE_MAX = 512
_cache_locais: OrderedDict[str, Local] = OrderedDict()

# Nominatim aceita no máximo 1 requisição por segundo
_nominatim_lock = asyncio.Lock()
_nominatim_ultima = 0.0


def _resultado_falha(mensagem: str, motivo: str = ERRO_SERVICO, **extra) -> dict:
    return {'sucesso': False, 'valor_frete': None, 'mensagem': mensagem, 'motivo': motivo, **extra}


def _aplicar_margem(valor: float) -> float:
    """Soma a margem configurada e arredonda para cima em R$ 0,50."""
    valor *= 1 + settings.FRETE_MARGEM_PERCENTUAL / 100
    return math.ceil(round(valor, 2) * 2) / 2


def calcular_valor_frete(distancia_km: float) -> float:
    """Aplica a tabela de preço configurada sobre a distância em km."""
    valor = settings.FRETE_TAXA_BASE + distancia_km * settings.FRETE_VALOR_KM
    return _aplicar_margem(max(valor, settings.FRETE_VALOR_MINIMO))


def _falha_distancia(distancia_km: float, local: Local) -> dict | None:
    if distancia_km <= settings.FRETE_DISTANCIA_MAXIMA_KM:
        return None
    return _resultado_falha(
        f'Endereço a {distancia_km:.1f} km ({local.endereco_formatado}), acima do '
        f'limite de {settings.FRETE_DISTANCIA_MAXIMA_KM:.0f} km.',
        FORA_AREA,
        distancia_km=distancia_km,
        endereco_formatado=local.endereco_formatado,
    )


def _descricao(distancia_km: float) -> str:
    return f'entrega moto, {distancia_km:.1f} km'.replace('.', ',')


def _normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize('NFKD', texto).encode('ascii', 'ignore').decode()
    return re.sub(r'\s+', ' ', sem_acento.lower()).strip()


def _cidade_padrao() -> str:
    """Cidade anexada a endereços sem cidade: FRETE_CIDADE_PADRAO ou o último
    trecho de ENDERECO_LOJA ("..., Bairro, Cidade - UF" -> "Cidade - UF")."""
    if settings.FRETE_CIDADE_PADRAO:
        return settings.FRETE_CIDADE_PADRAO
    partes = [p.strip() for p in settings.ENDERECO_LOJA.split(',') if p.strip()]
    return partes[-1] if len(partes) > 1 else ''


def _consultas(endereco: str) -> list[str]:
    """Endereço como veio e, se não citar a cidade da loja, com ela anexada."""
    consultas = [endereco]
    cidade = _cidade_padrao()
    nome_cidade = _normalizar(cidade.split(' - ')[0])
    if nome_cidade and nome_cidade not in _normalizar(endereco):
        consultas.append(f'{endereco}, {cidade}')
    return consultas


def _encurtar(endereco: str) -> str:
    """Tira país e região ("Região Sudeste") do endereço formatado do mapa."""
    partes = [
        p.strip() for p in endereco.split(',')
        if p.strip() and not re.fullmatch(r'(?i)brasil|brazil|regi[aã]o \w+', p.strip())
    ]
    return ', '.join(partes)


def _distancia_linha_reta_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * 6371 * math.asin(math.sqrt(h))


def _raio_graus() -> float:
    """Meia-largura (em graus) da caixa de busca em volta da loja."""
    return max(0.3, settings.FRETE_DISTANCIA_MAXIMA_KM * 1.5 / 111)


async def _requisitar(client: httpx.AsyncClient, metodo: str, url: str, **kwargs) -> httpx.Response:
    """Requisição com novas tentativas em timeout, 429 e 5xx."""
    for tentativa in range(1, _TENTATIVAS + 1):
        try:
            resp = await client.request(metodo, url, **kwargs)
            if resp.status_code not in _STATUS_RETENTAVEIS or tentativa == _TENTATIVAS:
                resp.raise_for_status()
                return resp
            logger.warning('%s respondeu %s, tentando de novo', url, resp.status_code)
        except httpx.TransportError as e:  # inclui timeouts
            if tentativa == _TENTATIVAS:
                raise
            logger.warning('%s falhou (%s), tentando de novo', url, e)
        await asyncio.sleep(1.5 * tentativa)
    raise AssertionError('inalcançável')


# ── Google Maps ──


async def _geocodificar_google(
    client: httpx.AsyncClient,
    endereco: str,
    perto_de: tuple[float, float] | None = None,
) -> Local:
    params = {
        'address': endereco,
        'key': settings.GOOGLE_MAPS_API_KEY,
        'region': 'br',
        'language': 'pt-BR',
        'components': 'country:BR',
    }
    if perto_de:
        lat, lng = perto_de
        r = _raio_graus()
        params['bounds'] = f'{lat - r},{lng - r}|{lat + r},{lng + r}'

    dados = (await _requisitar(client, 'GET', _GOOGLE_GEOCODE_URL, params=params)).json()
    status = dados.get('status')
    if status == 'ZERO_RESULTS':
        raise FreteErro(f'Endereço não encontrado: {endereco}', NAO_ENCONTRADO)
    if status != 'OK' or not dados.get('results'):
        raise FreteErro(f'Google Geocoding: {status} {dados.get("error_message", "")}')

    resultado = dados['results'][0]
    if set(resultado.get('types', [])) <= _TIPOS_IMPRECISOS_GOOGLE:
        raise FreteErro(
            f'Endereço impreciso (só achou "{resultado.get("formatted_address")}")',
            NAO_ENCONTRADO,
        )
    local = resultado['geometry']['location']
    return Local(local['lat'], local['lng'], _encurtar(resultado.get('formatted_address', endereco)))


async def _distancia_google(client: httpx.AsyncClient, origem: Local, destino: Local) -> float:
    def ponto(local: Local) -> dict:
        return {'location': {'latLng': {'latitude': local.lat, 'longitude': local.lng}}}

    resp = await _requisitar(
        client, 'POST', _GOOGLE_ROUTES_URL,
        headers={
            'X-Goog-Api-Key': settings.GOOGLE_MAPS_API_KEY,
            'X-Goog-FieldMask': 'routes.distanceMeters',
        },
        json={
            'origin': ponto(origem),
            'destination': ponto(destino),
            'travelMode': 'DRIVE',
            'languageCode': 'pt-BR',
            'regionCode': 'br',
        },
    )
    rotas = resp.json().get('routes') or []
    if not rotas or 'distanceMeters' not in rotas[0]:
        raise FreteErro('Google Routes não retornou rota.')
    return rotas[0]['distanceMeters'] / 1000


# ── OpenStreetMap (Nominatim + OSRM) ──


async def _nominatim(client: httpx.AsyncClient, params: dict) -> list:
    global _nominatim_ultima
    async with _nominatim_lock:
        espera = 1.1 - (time.monotonic() - _nominatim_ultima)
        if espera > 0:
            await asyncio.sleep(espera)
        try:
            resp = await _requisitar(client, 'GET', _NOMINATIM_URL, params=params)
        finally:
            _nominatim_ultima = time.monotonic()
    return resp.json()


async def _geocodificar_osm(
    client: httpx.AsyncClient,
    endereco: str,
    perto_de: tuple[float, float] | None = None,
) -> Local:
    """Tenta o endereço completo e depois sem número/CEP, já que muitos
    números de casa não estão mapeados no OSM. Com `perto_de`, só aceita
    resultados na região da loja."""
    params = {'format': 'jsonv2', 'countrycodes': 'br', 'limit': 1}
    if perto_de:
        lat, lon = perto_de
        r = _raio_graus()
        params['viewbox'] = f'{lon - r},{lat + r},{lon + r},{lat - r}'
        params['bounded'] = 1

    sem_numero = re.sub(r'\b\d{5}-?\d{3}\b|\b\d{1,5}\b', '', endereco)
    sem_numero = re.sub(r'\s*,\s*(,\s*)+', ', ', sem_numero).strip(' ,-')

    for consulta in dict.fromkeys([endereco, sem_numero]):  # sem duplicar
        if not consulta:
            continue
        resultados = await _nominatim(client, {**params, 'q': consulta})
        if resultados and resultados[0].get('addresstype') not in _TIPOS_IMPRECISOS_OSM:
            r0 = resultados[0]
            return Local(float(r0['lat']), float(r0['lon']), _encurtar(r0.get('display_name', consulta)))

    raise FreteErro(f'Endereço não encontrado no mapa: {endereco}', NAO_ENCONTRADO)


async def _distancia_osm(client: httpx.AsyncClient, origem: Local, destino: Local) -> float:
    resp = await _requisitar(
        client, 'GET',
        f'{_OSRM_URL}/{origem.lng},{origem.lat};{destino.lng},{destino.lat}',
        params={'overview': 'false'},
    )
    dados = resp.json()
    if dados.get('code') != 'Ok' or not dados.get('routes'):
        raise FreteErro(f'OSRM sem rota: {dados.get("code")}')
    return dados['routes'][0]['distance'] / 1000


# ── Localização (Google com fallback para OSM) ──


async def _geocodificar(
    client: httpx.AsyncClient,
    endereco: str,
    perto_de: tuple[float, float] | None = None,
) -> Local:
    if settings.GOOGLE_MAPS_API_KEY:
        try:
            return await _geocodificar_google(client, endereco, perto_de=perto_de)
        except FreteErro as e:
            if e.motivo == NAO_ENCONTRADO:
                logger.info('Google não achou "%s", tentando OSM', endereco)
            else:
                logger.warning('Google Geocoding falhou (%s), tentando OSM', e)
        except Exception as e:
            logger.warning('Google Geocoding falhou (%s), tentando OSM', e)
    return await _geocodificar_osm(client, endereco, perto_de=perto_de)


async def _obter_loja(client: httpx.AsyncClient) -> Local:
    global _loja
    if _loja is None:
        if settings.LOJA_COORDENADAS:
            lat, lng = (float(v) for v in settings.LOJA_COORDENADAS.split(','))
            _loja = Local(lat, lng, settings.ENDERECO_LOJA)
        else:
            _loja = await _geocodificar(client, settings.ENDERECO_LOJA)
            logger.info(
                'Loja geocodificada em %.6f,%.6f (%s). Fixe LOJA_COORDENADAS no .env '
                'para não depender disso.', _loja.lat, _loja.lng, _loja.endereco_formatado,
            )
    return _loja


async def localizar_cliente(client: httpx.AsyncClient, endereco: str) -> Local:
    """Coordenadas do endereço do cliente, preferindo a região da loja.

    Tenta o texto como veio e com a cidade da loja anexada; fica com o primeiro
    resultado dentro da área de entrega (linha reta) ou, se nenhum estiver, o
    mais próximo — que depois cai no limite de distância.
    """
    chave = _normalizar(endereco)
    if chave in _cache_locais:
        _cache_locais.move_to_end(chave)
        return _cache_locais[chave]

    loja = await _obter_loja(client)
    candidatos: list[tuple[float, Local]] = []
    ultimo_erro: FreteErro | None = None
    for consulta in _consultas(endereco):
        try:
            local = await _geocodificar(client, consulta, perto_de=loja.coords)
        except FreteErro as e:
            ultimo_erro = e
            continue
        km = _distancia_linha_reta_km(loja.coords, local.coords)
        if km <= settings.FRETE_DISTANCIA_MAXIMA_KM:
            candidatos = [(km, local)]
            break
        candidatos.append((km, local))

    if not candidatos:
        raise ultimo_erro or FreteErro(f'Endereço não encontrado: {endereco}', NAO_ENCONTRADO)

    _, local = min(candidatos, key=lambda c: c[0])
    _cache_locais[chave] = local
    while len(_cache_locais) > _CACHE_MAX:
        _cache_locais.popitem(last=False)
    logger.info('Endereço "%s" -> %s (%.6f,%.6f)', endereco, local.endereco_formatado, local.lat, local.lng)
    return local


async def _distancia_rota(client: httpx.AsyncClient, origem: Local, destino: Local) -> float:
    if settings.GOOGLE_MAPS_API_KEY:
        try:
            return await _distancia_google(client, origem, destino)
        except Exception as e:
            logger.warning('Google Routes falhou (%s), tentando OSRM', e)
    try:
        return await _distancia_osm(client, origem, destino)
    except Exception as e:
        logger.warning('OSRM indisponível (%s), usando linha reta x %.2f', e, _FATOR_ROTA)
        return _distancia_linha_reta_km(origem.coords, destino.coords) * _FATOR_ROTA


def _novo_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=15, headers={'User-Agent': _USER_AGENT})


async def _localizar(endereco_cliente: str) -> tuple[Local, Local] | dict:
    """(loja, cliente) ou um dict de falha pronto para devolver."""
    try:
        async with _novo_client() as client:
            return await _obter_loja(client), await localizar_cliente(client, endereco_cliente)
    except FreteErro as e:
        logger.warning('Endereço não localizado: %s', e)
        return _resultado_falha(str(e), e.motivo)
    except Exception:
        logger.exception('Erro ao localizar endereço')
        return _resultado_falha('Erro no serviço de mapas ao localizar o endereço.')


# ── Estimativa por distância ──


async def estimar_frete(endereco_cliente: str, locais: tuple[Local, Local] | None = None) -> dict:
    """Estima o frete pela distância de rota loja -> cliente.

    Returns:
        dict com sucesso, valor_frete, mensagem, motivo (em falhas) e, em caso
        de sucesso, distancia_km, endereco_formatado e fornecedor.
    """
    if locais is None:
        locais = await _localizar(endereco_cliente)
        if isinstance(locais, dict):
            return locais
    loja, cliente = locais

    try:
        async with _novo_client() as client:
            distancia_km = await _distancia_rota(client, loja, cliente)
    except Exception:
        logger.exception('Erro ao calcular rota')
        return _resultado_falha('Erro no serviço de mapas ao calcular a rota.')

    if falha := _falha_distancia(distancia_km, cliente):
        return falha

    valor = calcular_valor_frete(distancia_km)
    logger.info('Frete estimado: %.1f km -> R$ %.2f', distancia_km, valor)
    return {
        'sucesso': True,
        'valor_frete': valor,
        'distancia_km': distancia_km,
        'endereco_formatado': cliente.endereco_formatado,
        'fornecedor': 'estimativa',
        'mensagem': _descricao(distancia_km),
    }


# ── Lalamove ──


async def cotar_frete_lalamove(
    endereco_cliente: str, locais: tuple[Local, Local] | None = None,
) -> dict:
    """Cota o frete na API da Lalamove (preço real da moto)."""
    if locais is None:
        locais = await _localizar(endereco_cliente)
        if isinstance(locais, dict):
            return locais
    loja, cliente = locais

    logger.info('Cotando Lalamove: %s -> %s', loja.endereco_formatado, cliente.endereco_formatado)
    try:
        async with _novo_client() as client:
            cotacao = await lalamove.cotar(
                client,
                origem=(settings.ENDERECO_LOJA, loja.coords),
                destino=(cliente.endereco_formatado, cliente.coords),
            )
    except lalamove.LalamoveErro as e:
        logger.warning('Lalamove não cotou: %s', e)
        return _resultado_falha(str(e))
    except Exception:
        logger.exception('Erro ao cotar na Lalamove')
        return _resultado_falha('Erro ao cotar na Lalamove.')

    distancia_km = cotacao['distancia_km']
    if falha := _falha_distancia(distancia_km, cliente):
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
        'endereco_formatado': cliente.endereco_formatado,
        'fornecedor': 'lalamove',
        'custo_fornecedor': cotacao['valor'],
        'cotacao_id': cotacao['cotacao_id'],
        'mensagem': _descricao(distancia_km),
    }


# ── Ponto de entrada ──


async def cotar_frete(endereco_cliente: str) -> dict:
    """Cota o frete conforme FRETE_MODO ('lalamove', 'estimativa' ou 'uber_scraper')."""
    if settings.FRETE_MODO == 'uber_scraper':
        from .scraper import cotar_frete_uber
        return await cotar_frete_uber(endereco_cliente)

    locais = await _localizar(endereco_cliente)
    if isinstance(locais, dict):
        return locais

    if settings.FRETE_MODO == 'lalamove':
        resultado = await cotar_frete_lalamove(endereco_cliente, locais)
        if resultado['sucesso'] or resultado['motivo'] == FORA_AREA:
            return resultado
        logger.warning(
            'Lalamove falhou (%s); usando estimativa por distância',
            resultado['mensagem'],
        )

    return await estimar_frete(endereco_cliente, locais)
