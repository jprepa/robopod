"""Manda as notificações para os celulares da equipe (roda sem parar no
serviço `notificacoes` do docker-compose).

    python manage.py enviar_notificacoes                 # a cada 10 s
    python manage.py enviar_notificacoes --intervalo 5

A cada volta compara o banco com a volta anterior (pedido novo, comprovante,
pagamento, cliente pedindo atendente, mensagem nova) e avisa os aparelhos
inscritos. Sem as chaves VAPID no .env, só espera.
"""

import logging
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from painel import push

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Verifica novidades e manda as notificações para os celulares (não para).'

    def add_arguments(self, parser):
        parser.add_argument('--intervalo', type=float, default=10, help='Segundos entre as verificações')

    def handle(self, *args, intervalo, **opts):
        if not push.ativo():
            self.stdout.write('Notificações desligadas: defina VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY e '
                              'VAPID_CONTATO (ou PAINEL_URL) no .env. Esperando.')
            while True:
                time.sleep(3600)

        self.stdout.write(f'Notificações ligadas: verificando a cada {intervalo:g} s.')
        verificador = push.Verificador()
        while True:
            # O banco pode ter reiniciado: descarta conexões quebradas antes de cada volta
            close_old_connections()
            try:
                avisos = verificador.verificar()
                if avisos:
                    enviados = push.distribuir(avisos)
                    self.stdout.write(f'{len(avisos)} novidade(s), {enviados} envio(s)')
            except Exception:
                logger.exception('Notificações: falha na verificação')
            time.sleep(intervalo)
