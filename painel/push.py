"""Notificações no celular (Web Push): quem recebe, o que verificar e o envio.

O serviço `notificacoes` do docker-compose (python manage.py enviar_notificacoes)
chama `Verificador.verificar()` a cada poucos segundos. Ele olha o banco do
mesmo jeito que os sons do painel (ver `_marcas_alerta` em views.py) e manda
um aviso para cada aparelho inscrito que quer aquele tipo de evento.

O aviso mostra o tipo do evento e o nome do cliente, nunca o texto da
mensagem: ele aparece na tela bloqueada.
"""

import json
import logging
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from core.models import SessaoChat

from .models import Cliente, InscricaoPush, Mensagem, Pedido

logger = logging.getLogger(__name__)

# Mesmos eventos (e mesmos padrões) dos sons de alerta em base.html
EVENTOS = {
    'atendente': ('Cliente pediu atendente', True),
    'pago': ('Pagamento confirmado', True),
    'comprovante': ('Comprovante de pagamento recebido', True),
    'pedido': ('Novo pedido', True),
    'mensagem_humano': ('Mensagem de cliente em atendimento humano', True),
    'mensagem': ('Qualquer mensagem de cliente (inclusive as que o bot responde)', False),
}
EVENTOS_PADRAO = [k for k, (_, padrao) in EVENTOS.items() if padrao]

# Só manda para os serviços de push dos navegadores (evita o servidor ser
# usado para chamar endereços internos)
_SERVICOS_PUSH = (
    'push.apple.com', 'fcm.googleapis.com', 'android.googleapis.com',
    'push.services.mozilla.com', 'notify.windows.com',
)


def ativo() -> bool:
    return bool(settings.VAPID_PUBLIC_KEY and settings.VAPID_PRIVATE_KEY and settings.VAPID_CONTATO)


def endpoint_valido(endpoint: str) -> bool:
    partes = urlsplit(endpoint or '')
    host = (partes.hostname or '').lower()
    return partes.scheme == 'https' and any(host == s or host.endswith('.' + s) for s in _SERVICOS_PUSH)


def _contato() -> str:
    c = settings.VAPID_CONTATO
    return c if c.startswith(('mailto:', 'https://')) else f'mailto:{c}'


def enviar(inscricao: InscricaoPush, dados: dict) -> bool:
    """Manda um aviso para um aparelho. Apaga a inscrição se o aparelho saiu."""
    from pywebpush import WebPushException, webpush

    try:
        webpush(
            subscription_info={'endpoint': inscricao.endpoint,
                               'keys': {'p256dh': inscricao.p256dh, 'auth': inscricao.auth}},
            data=json.dumps(dados, ensure_ascii=False),
            vapid_private_key=settings.VAPID_PRIVATE_KEY,
            vapid_claims={'sub': _contato()},
            ttl=6 * 60 * 60,
            headers={'Urgency': 'high'},
            timeout=10,
        )
    except WebPushException as e:
        status = getattr(e.response, 'status_code', None)
        if status in (404, 410):
            logger.info('Notificações: %s saiu (HTTP %s), inscrição apagada', inscricao, status)
            inscricao.delete()
        else:
            logger.warning('Notificações: falha ao enviar para %s: %s', inscricao, e)
        return False
    except Exception as e:  # rede fora, timeout
        logger.warning('Notificações: falha ao enviar para %s: %s', inscricao, e)
        return False
    InscricaoPush.objects.filter(pk=inscricao.pk).update(ultimo_envio=timezone.now())
    return True


def _contador_icone() -> int:
    """Número no ícone do app: o mesmo total do título da aba do painel."""
    return (SessaoChat.objects.filter(estado_atual=SessaoChat.Estado.ATENDIMENTO_HUMANO).count()
            + Pedido.objects.filter(status=Pedido.Status.COMPROVANTE_RECEBIDO).count())


def _nomes(telefones) -> dict:
    nomes = {c.telefone: c.nome_exibicao for c in Cliente.objects.filter(telefone__in=telefones)}
    return {t: nomes.get(t) or t for t in telefones}


def _aviso_pedido(evento: str, pedido: Pedido) -> dict:
    titulo = {
        'pedido': f'Novo pedido #{pedido.pk}',
        'comprovante': f'Comprovante recebido · pedido #{pedido.pk}',
        'pago': f'Pagamento confirmado · pedido #{pedido.pk}',
    }[evento]
    nome = pedido.cliente.nome_exibicao if pedido.cliente else 'Cliente'
    total = f'R$ {pedido.total:.2f}'.replace('.', ',')
    return {'titulo': titulo, 'corpo': f'{nome} · {total}',
            'url': reverse('painel:pedido', args=[pedido.pk]), 'tag': f'pedido-{pedido.pk}'}


def _aviso_conversa(evento: str, telefone: str, nome: str, qtd: int = 1) -> dict:
    if evento == 'atendente':
        titulo, corpo = 'Cliente pediu atendente', nome
    else:
        titulo = f'Mensagem de {nome}'
        if evento == 'mensagem_humano':
            corpo = 'Em atendimento humano' if qtd == 1 else f'{qtd} mensagens · em atendimento humano'
        else:
            corpo = 'O bot está respondendo' if qtd == 1 else f'{qtd} mensagens · o bot está respondendo'
    return {'titulo': titulo, 'corpo': corpo,
            'url': reverse('painel:conversa', args=[telefone]), 'tag': f'conversa-{telefone}'}


class Verificador:
    """Guarda em memória o que já foi visto e devolve só as novidades.

    Na primeira chamada só registra o estado atual: reiniciar o serviço não
    repete avisos antigos.
    """

    def __init__(self):
        self.vistos = None

    def _estado(self) -> dict:
        recentes = timezone.now() - timedelta(days=2)
        # Vendas lançadas à mão no painel não notificam: quem lançou já sabe
        pedidos = Pedido.objects.exclude(origem=Pedido.Origem.MANUAL)
        ids_com_status = lambda s: set(  # noqa: E731
            pedidos.filter(status=s, atualizado_em__gte=recentes).values_list('pk', flat=True))
        ultimo = lambda qs: qs.order_by('-pk').values_list('pk', flat=True).first() or 0  # noqa: E731
        return {
            'pedido': ultimo(pedidos),
            'comprovante': ids_com_status(Pedido.Status.COMPROVANTE_RECEBIDO),
            'pago': ids_com_status(Pedido.Status.PAGO),
            'atendente': set(SessaoChat.objects.filter(
                estado_atual=SessaoChat.Estado.ATENDIMENTO_HUMANO).values_list('telefone', flat=True)),
            'mensagem': ultimo(Mensagem.objects.filter(autor=Mensagem.Autor.CLIENTE)),
        }

    def verificar(self) -> list[tuple[str, dict]]:
        """Lista de (evento, aviso) com o que aconteceu desde a última chamada."""
        atual = self._estado()
        antes, self.vistos = self.vistos, atual
        if antes is None:
            return []

        avisos = []
        # Pedido: cada um entra uma vez só, no estágio mais avançado que tiver agora
        ids = {
            'pago': atual['pago'] - antes['pago'],
            'comprovante': atual['comprovante'] - antes['comprovante'],
            'pedido': set(Pedido.objects.exclude(origem=Pedido.Origem.MANUAL).filter(
                pk__gt=antes['pedido']).values_list('pk', flat=True)),
        }
        ja_avisados = set()
        for evento in ('pago', 'comprovante', 'pedido'):
            ids[evento] -= ja_avisados
            ja_avisados |= ids[evento]
        todos_ids = set().union(*ids.values())
        if todos_ids:
            por_id = Pedido.objects.select_related('cliente').in_bulk(todos_ids)
            for evento in ('pago', 'comprovante', 'pedido'):
                avisos += [(evento, _aviso_pedido(evento, por_id[i])) for i in sorted(ids[evento]) if i in por_id]

        # Conversa: "pediu atendente" vale mais que a mensagem que veio junto
        novos_atendente = atual['atendente'] - antes['atendente']
        msgs = {}
        for tel in Mensagem.objects.filter(
            autor=Mensagem.Autor.CLIENTE, pk__gt=antes['mensagem'],
        ).values_list('telefone', flat=True):
            msgs[tel] = msgs.get(tel, 0) + 1
        nomes = _nomes(novos_atendente | set(msgs))
        avisos += [('atendente', _aviso_conversa('atendente', t, nomes[t])) for t in sorted(novos_atendente)]
        for tel, qtd in msgs.items():
            if tel in novos_atendente:
                continue
            evento = 'mensagem_humano' if tel in atual['atendente'] else 'mensagem'
            avisos.append((evento, _aviso_conversa(evento, tel, nomes[tel], qtd)))
        return avisos


def distribuir(avisos: list[tuple[str, dict]]) -> int:
    """Manda cada aviso para os aparelhos que querem aquele evento. Devolve quantos saíram."""
    if not avisos:
        return 0
    inscricoes = list(InscricaoPush.objects.select_related('usuario').filter(usuario__is_active=True))
    if not inscricoes:
        return 0
    icone = _contador_icone()
    enviados = 0
    for evento, aviso in avisos:
        for inscricao in inscricoes:
            if inscricao.pk is None:  # apagada num envio anterior (aparelho saiu)
                continue
            if evento in inscricao.eventos and enviar(inscricao, {**aviso, 'contador': icone}):
                enviados += 1
    return enviados
