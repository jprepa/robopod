"""Gera o par de chaves das notificações no celular (VAPID).

    python manage.py gerar_chaves_push

Cole as duas linhas no .env. Gere só uma vez: trocar as chaves desliga as
notificações de todos os celulares, e cada um precisa ativar de novo.
"""

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.core.management.base import BaseCommand


def _b64(dados: bytes) -> str:
    return base64.urlsafe_b64encode(dados).rstrip(b'=').decode()


class Command(BaseCommand):
    help = 'Gera VAPID_PUBLIC_KEY e VAPID_PRIVATE_KEY para as notificações no celular.'

    def handle(self, *args, **opts):
        chave = ec.generate_private_key(ec.SECP256R1())
        privada = chave.private_numbers().private_value.to_bytes(32, 'big')
        publica = chave.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint,
        )
        self.stdout.write('Cole no .env (gere só uma vez):\n')
        self.stdout.write(f'VAPID_PUBLIC_KEY={_b64(publica)}')
        self.stdout.write(f'VAPID_PRIVATE_KEY={_b64(privada)}')
