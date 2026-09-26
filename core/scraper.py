"""Scraper assíncrono para cotação de frete via Uber (m.uber.com).

Utiliza Playwright para preencher endereços e raspar o valor
da opção "Envios Moto" na interface mobile da Uber.
"""

import logging
import os
import re

from django.conf import settings
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# Em VPS sem display, DEVE ser True. Para debug local, setar PLAYWRIGHT_HEADLESS=false
HEADLESS = os.getenv('PLAYWRIGHT_HEADLESS', 'true').lower() in ('true', '1', 'yes')

logger = logging.getLogger(__name__)

UBER_URL = 'https://m.uber.com/go/delivery'


async def cotar_frete_uber(endereco_cliente: str) -> dict:
    """Cota o frete no Uber via scraping de m.uber.com.

    Args:
        endereco_cliente: Endereço completo do cliente (rua, número, bairro, cidade).

    Returns:
        dict com:
            - sucesso (bool)
            - valor_frete (float | None): valor em reais
            - mensagem (str): mensagem de status ou erro
    """
    endereco_loja = settings.ENDERECO_LOJA

    logger.info(
        'Iniciando cotação Uber: %s -> %s',
        endereco_loja,
        endereco_cliente,
    )

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=HEADLESS)
            context = await browser.new_context(
                locale='pt-BR',
                viewport={'width': 375, 'height': 812},
                user_agent=(
                    'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) '
                    'AppleWebKit/605.1.15 (KHTML, like Gecko) '
                    'Version/16.0 Mobile/15E148 Safari/604.1'
                ),
            )
            page = await context.new_page()

            # ── Acessa a página de delivery ──
            await page.goto(UBER_URL, wait_until='networkidle', timeout=30_000)
            await page.wait_for_timeout(2000)

            # ── Preenche endereço de ORIGEM (loja) ──
            pickup_input = page.locator(
                'input[placeholder*="Retirada"], '
                'input[placeholder*="Pickup"], '
                'input[data-testid="pickup-input"]'
            ).first
            await pickup_input.click()
            await pickup_input.fill(endereco_loja)
            await page.wait_for_timeout(1500)

            # Seleciona primeira sugestão de endereço
            primeira_sugestao = page.locator(
                '[data-testid="location-suggestion"], '
                'li[role="option"], '
                'div[class*="suggestion"]'
            ).first
            await primeira_sugestao.click()
            await page.wait_for_timeout(1000)

            # ── Preenche endereço de DESTINO (cliente) ──
            dropoff_input = page.locator(
                'input[placeholder*="Entrega"], '
                'input[placeholder*="Dropoff"], '
                'input[data-testid="dropoff-input"]'
            ).first
            await dropoff_input.click()
            await dropoff_input.fill(endereco_cliente)
            await page.wait_for_timeout(1500)

            primeira_sugestao_dest = page.locator(
                '[data-testid="location-suggestion"], '
                'li[role="option"], '
                'div[class*="suggestion"]'
            ).first
            await primeira_sugestao_dest.click()
            await page.wait_for_timeout(2000)

            # ── Aguarda o mapa e as opções de entrega ──
            await page.wait_for_selector(
                '[data-testid="fare-estimate"], '
                'div[class*="fare"], '
                'div[class*="price"]',
                timeout=15_000,
            )
            await page.wait_for_timeout(2000)

            # ── Raspa o valor de "Envios Moto" ──
            opcoes = await page.locator(
                '[data-testid="vehicle-option"], '
                'div[class*="vehicle-option"], '
                'div[class*="product-option"]'
            ).all()

            valor_moto = None
            for opcao in opcoes:
                texto = await opcao.inner_text()
                if 'moto' in texto.lower() or 'envios moto' in texto.lower():
                    # Extrai valor numérico (ex: "R$ 12,50" -> 12.50)
                    match = re.search(r'R\$\s*([\d.,]+)', texto)
                    if match:
                        valor_str = match.group(1).replace('.', '').replace(',', '.')
                        valor_moto = float(valor_str)
                        break

            await browser.close()

            if valor_moto is not None:
                logger.info('Frete cotado: R$ %.2f', valor_moto)
                return {
                    'sucesso': True,
                    'valor_frete': valor_moto,
                    'mensagem': f'Frete Uber Moto: R$ {valor_moto:.2f}',
                }

            # Fallback: tenta raspar qualquer valor na página
            page_text = await page.locator('body').inner_text()
            match = re.search(r'R\$\s*([\d.,]+)', page_text)
            if match:
                valor_str = match.group(1).replace('.', '').replace(',', '.')
                valor_fallback = float(valor_str)
                logger.warning('Fallback frete: R$ %.2f', valor_fallback)
                return {
                    'sucesso': True,
                    'valor_frete': valor_fallback,
                    'mensagem': f'Frete estimado: R$ {valor_fallback:.2f}',
                }

            return {
                'sucesso': False,
                'valor_frete': None,
                'mensagem': 'Não foi possível encontrar o valor do frete na página.',
            }

    except PlaywrightTimeout:
        logger.error('Timeout no scraping do Uber')
        return {
            'sucesso': False,
            'valor_frete': None,
            'mensagem': 'Timeout ao acessar a página do Uber. Tente novamente.',
        }
    except Exception:
        logger.exception('Erro inesperado no scraping do Uber')
        return {
            'sucesso': False,
            'valor_frete': None,
            'mensagem': 'Erro ao cotar frete. Um atendente irá ajudá-lo.',
        }
