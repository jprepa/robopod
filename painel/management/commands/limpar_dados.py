"""Apaga os dados dos clientes que já cumpriram o papel (roda todo dia pelo
serviço `limpeza` do docker-compose).

    python manage.py limpar_dados              # apaga
    python manage.py limpar_dados --simular    # só mostra quanto apagaria

- Endereço (e endereço no mapa) dos pedidos entregues, cancelados ou abandonados
  sem pagamento há mais de RETENCAO_ENDERECO_DIAS; os demais pedidos perdem o
  endereço depois de 30 dias, mesmo que o status tenha ficado esquecido.
- Comprovante que tenha sobrado em pedido pago, entregue ou cancelado.
- Histórico das conversas mais antigo que RETENCAO_MENSAGENS_DIAS: mensagens do
  painel, contexto da IA nas sessões paradas e mensagens guardadas pela Evolution.

O telefone e o nome do cliente ficam: são o que identifica o cliente.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.utils import timezone

from core.models import SessaoChat
from painel.models import Mensagem, Pedido

logger = logging.getLogger(__name__)

# Pedidos que ainda podem precisar do endereço (pagamento a conferir ou entrega a fazer)
_STATUS_EM_CURSO = (Pedido.Status.COMPROVANTE_RECEBIDO, Pedido.Status.PAGO, Pedido.Status.EM_ENTREGA)
_LIMITE_STATUS_ESQUECIDO_DIAS = 30


class Command(BaseCommand):
    help = 'Apaga endereços, comprovantes e conversas antigas dos clientes.'

    def add_arguments(self, parser):
        parser.add_argument('--simular', action='store_true', help='Só mostra quanto seria apagado')

    def handle(self, *args, **opts):
        self.simular = opts['simular']
        agora = timezone.now()
        limite_endereco = agora - timedelta(days=settings.RETENCAO_ENDERECO_DIAS)
        limite_mensagens = agora - timedelta(days=settings.RETENCAO_MENSAGENS_DIAS)

        self._relatar('endereços de pedidos', self._limpar_enderecos(agora, limite_endereco))
        self._relatar('comprovantes', self._limpar_comprovantes())
        self._relatar('mensagens do painel', self._apagar(
            Mensagem.objects.filter(criado_em__lt=limite_mensagens),
        ))
        self._relatar('históricos da IA', self._limpar_sessoes(limite_mensagens))
        self._relatar('mensagens da Evolution', self._limpar_evolution(limite_mensagens))

    def _relatar(self, o_que: str, quantidade: int | None) -> None:
        if quantidade is None:
            return
        verbo = 'seriam apagados' if self.simular else 'apagados'
        self.stdout.write(f'{o_que}: {quantidade} {verbo}')
        if quantidade and not self.simular:
            logger.info('limpar_dados: %s %s apagados', quantidade, o_que)

    def _apagar(self, queryset) -> int:
        quantidade = queryset.count()
        if quantidade and not self.simular:
            queryset.delete()
        return quantidade

    def _limpar_enderecos(self, agora, limite) -> int:
        com_endereco = Pedido.objects.exclude(endereco='', endereco_mapa='')
        vencidos = com_endereco.filter(atualizado_em__lt=limite).exclude(status__in=_STATUS_EM_CURSO) | (
            com_endereco.filter(atualizado_em__lt=agora - timedelta(days=_LIMITE_STATUS_ESQUECIDO_DIAS))
        )
        quantidade = vencidos.count()
        if quantidade and not self.simular:
            # update() não mexe no atualizado_em (auto_now)
            Pedido.objects.filter(pk__in=vencidos.values('pk')).update(endereco='', endereco_mapa='')
        return quantidade

    def _limpar_comprovantes(self) -> int:
        sobrando = Pedido.objects.exclude(comprovante='').filter(
            status__in=(*Pedido.STATUS_FATURADOS, Pedido.Status.CANCELADO),
        )
        quantidade = 0
        for pedido in sobrando:
            quantidade += 1
            if not self.simular:
                pedido.comprovante.delete(save=False)
                Pedido.objects.filter(pk=pedido.pk).update(comprovante='')
        return quantidade

    def _limpar_sessoes(self, limite) -> int:
        """Sessões paradas: tira o contexto da IA e o endereço do carrinho, sem
        mudar o estado da conversa."""
        quantidade = 0
        for sessao in SessaoChat.objects.filter(ultima_interacao__lt=limite):
            if not sessao.historico_mensagens and 'endereco' not in sessao.carrinho_temporario:
                continue
            quantidade += 1
            if not self.simular:
                sessao.carrinho_temporario.pop('endereco', None)
                SessaoChat.objects.filter(pk=sessao.pk).update(
                    historico_mensagens=[], carrinho_temporario=sessao.carrinho_temporario,
                )
        return quantidade

    def _limpar_evolution(self, limite) -> int | None:
        """Mensagens que a Evolution guarda no schema "evolution" do mesmo banco
        (DATABASE_SAVE_DATA_NEW_MESSAGE). A mídia do comprovante/áudio é baixada
        na hora em que chega, então mensagens antigas não fazem falta."""
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('evolution.\"Message\"')")
            if cursor.fetchone()[0] is None:
                return None  # Evolution não guarda mensagens neste banco
            corte = int(limite.timestamp())
            cursor.execute('SELECT count(*) FROM evolution."Message" WHERE "messageTimestamp" < %s', [corte])
            quantidade = cursor.fetchone()[0]
            if not quantidade or self.simular:
                return quantidade
            try:
                with transaction.atomic():
                    cursor.execute('DELETE FROM evolution."Message" WHERE "messageTimestamp" < %s', [corte])
            except Exception:
                logger.exception('limpar_dados: falha ao apagar as mensagens da Evolution')
                self.stderr.write('Falha ao apagar as mensagens da Evolution (veja o log)')
                return None
        return quantidade
