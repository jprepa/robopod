"""Testa a cotação de frete sem passar pelo WhatsApp.

    python manage.py testar_frete "Rua X, 100, Botafogo, Rio de Janeiro"
    python manage.py testar_frete --servicos   # serviços da Lalamove por cidade
"""

import asyncio
import json

import httpx
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core import lalamove
from core.frete import cotar_frete, cotar_frete_lalamove, estimar_frete


class Command(BaseCommand):
    help = 'Cota o frete para um endereço usando o FRETE_MODO atual.'

    def add_arguments(self, parser):
        parser.add_argument('endereco', nargs='?', help='Endereço do cliente')
        parser.add_argument(
            '--modo', choices=['lalamove', 'estimativa'],
            help='Força um fornecedor, sem fallback (padrão: FRETE_MODO)',
        )
        parser.add_argument(
            '--servicos', action='store_true',
            help='Lista cidades e serviceType da Lalamove no Brasil',
        )

    def handle(self, *args, endereco=None, modo=None, servicos=False, **options):
        if servicos:
            asyncio.run(self._listar_servicos())
            return
        if not endereco:
            raise CommandError('Informe o endereço ou use --servicos.')

        cotadores = {
            'lalamove': cotar_frete_lalamove,
            'estimativa': estimar_frete,
            None: cotar_frete,
        }
        self.stdout.write(
            f'Modo: {modo or settings.FRETE_MODO} | '
            f'Lalamove {"sandbox" if settings.LALAMOVE_SANDBOX else "produção"}',
        )
        resultado = asyncio.run(cotadores[modo](endereco))
        self.stdout.write(json.dumps(resultado, ensure_ascii=False, indent=2))

    async def _listar_servicos(self):
        async with httpx.AsyncClient(timeout=15) as client:
            for cidade in await lalamove.listar_servicos(client):
                self.stdout.write(
                    f"{cidade['cidade']} ({cidade['locode']}): "
                    f"{', '.join(cidade['servicos'])}",
                )
