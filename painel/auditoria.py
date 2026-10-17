"""Registro de auditoria: quem fez cada ação no painel e o que o bot fez
sozinho. Gravar nunca levanta erro: falhar a auditoria não pode impedir o
atendimento."""

import logging
from datetime import timedelta
from decimal import Decimal

from asgiref.sync import sync_to_async
from django import forms
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.db import transaction
from django.dispatch import receiver
from django.utils import timezone

from .models import RegistroAuditoria

logger = logging.getLogger(__name__)

Tipo = RegistroAuditoria.Tipo


def _alvo(alvo) -> tuple[str, str]:
    """Instância de modelo ou tupla (tipo, id) -> ('pedido', '42')."""
    if alvo is None:
        return '', ''
    if isinstance(alvo, tuple):
        return alvo[0], str(alvo[1])
    return alvo._meta.model_name, str(alvo.pk)


def _ip(request) -> str | None:
    # Atrás do Nginx (GUIA_DEPLOY_VPS.md) o IP real do navegador vem no X-Real-IP
    return request.headers.get('X-Real-IP') or request.META.get('REMOTE_ADDR') or None


def _gravar(campos: dict, agrupar_minutos: int = 0) -> None:
    try:
        # Savepoint: um erro aqui não quebra a transação de quem chamou
        with transaction.atomic():
            if agrupar_minutos and campos.get('usuario') and _agrupar(campos, agrupar_minutos):
                return
            RegistroAuditoria.objects.create(**campos)
    except Exception:
        logger.exception('Falha ao gravar auditoria: %s', campos.get('descricao'))


def _agrupar(campos: dict, minutos: int) -> bool:
    """Junta cliques seguidos do mesmo usuário no mesmo item (ex.: +1 +1 +1 no
    estoque) num único registro com o 'antes' do primeiro e o 'depois' do último."""
    anterior = (
        RegistroAuditoria.objects
        .filter(
            usuario=campos['usuario'], acao=campos['acao'],
            alvo_tipo=campos['alvo_tipo'], alvo_id=campos['alvo_id'],
            descricao=campos['descricao'],
            criado_em__gte=timezone.now() - timedelta(minutes=minutos),
        )
        .order_by('-criado_em', '-pk').first()
    )
    if anterior is None:
        return False
    mudancas = dict(anterior.mudancas)
    for campo, (antes, depois) in campos['mudancas'].items():
        mudancas[campo] = [mudancas.get(campo, [antes])[0], depois]
    anterior.mudancas = {c: v for c, v in mudancas.items() if v[0] != v[1]}
    anterior.criado_em = timezone.now()
    anterior.save(update_fields=['mudancas', 'criado_em'])
    return True


def registrar(
    request, tipo: str, acao: str, descricao: str, *,
    alvo=None, mudancas: dict | None = None, agrupar_minutos: int = 0, usuario=None,
) -> None:
    """Ação feita por um usuário logado no painel."""
    usuario = usuario or request.user
    alvo_tipo, alvo_id = _alvo(alvo)
    _gravar({
        'usuario': usuario, 'responsavel': usuario.get_username(),
        'tipo': tipo, 'acao': acao, 'descricao': descricao[:255],
        'mudancas': mudancas or {}, 'alvo_tipo': alvo_tipo, 'alvo_id': alvo_id,
        'ip': _ip(request),
    }, agrupar_minutos)


def registrar_bot(tipo: str, acao: str, descricao: str, *, alvo=None, mudancas: dict | None = None) -> None:
    """Ação feita pelo bot, sem ninguém no painel."""
    alvo_tipo, alvo_id = _alvo(alvo)
    _gravar({
        'responsavel': RegistroAuditoria.BOT, 'tipo': tipo, 'acao': acao,
        'descricao': descricao[:255], 'mudancas': mudancas or {},
        'alvo_tipo': alvo_tipo, 'alvo_id': alvo_id,
    })


aregistrar_bot = sync_to_async(registrar_bot)


# ── Mudanças (antes → depois) ──


def texto(valor) -> str:
    """Valor de campo em texto para guardar no registro."""
    if valor is None or valor == '':
        return '—'
    if isinstance(valor, bool):
        return 'Sim' if valor else 'Não'
    if isinstance(valor, Decimal):
        return f'{valor:.2f}'.replace('.', ',')
    return str(valor)


# Campos sem verbose_name nos modelos: rótulo com acento para a tela
_ROTULOS = {
    'descricao': 'Descrição', 'preco': 'Preço', 'estoque_minimo': 'Estoque mínimo',
    'imagem_url': 'Imagem', 'observacoes': 'Observações',
}


def mudancas_form(form: forms.ModelForm, prefixo: str = '') -> dict:
    """{rótulo: [antes, depois]} dos campos alterados num ModelForm validado."""
    mudancas = {}
    for nome in form.changed_data:
        campo = form.fields[nome]
        if nome == 'DELETE':
            continue
        antes = form.initial.get(nome)
        # FK: o initial é o id; mostra o nome do objeto
        if isinstance(campo, forms.ModelChoiceField) and antes is not None:
            antes = campo.queryset.filter(pk=antes).first() or antes
        rotulo = _ROTULOS.get(nome) or str(campo.label or nome).capitalize()
        mudancas[f'{prefixo}{rotulo}'] = [texto(antes), texto(form.cleaned_data.get(nome))]
    return {r: v for r, v in mudancas.items() if v[0] != v[1]}


# ── Login e logout ──


@receiver(user_logged_in)
def _login(sender, request, user, **kwargs):
    if request is not None:
        registrar(request, Tipo.ACESSO, 'login', 'Entrou no painel', usuario=user)


@receiver(user_logged_out)
def _logout(sender, request, user, **kwargs):
    if request is not None and user is not None:
        registrar(request, Tipo.ACESSO, 'logout', 'Saiu do painel', usuario=user)


@receiver(user_login_failed)
def _login_falhou(sender, credentials, request=None, **kwargs):
    usuario = str(credentials.get('username') or '?')[:150]
    _gravar({
        'responsavel': usuario, 'tipo': Tipo.ACESSO, 'acao': 'login_falhou',
        'descricao': 'Login recusado (usuário ou senha incorretos)',
        'ip': _ip(request) if request is not None else None,
    })
