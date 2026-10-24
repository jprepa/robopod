"""Login do painel/admin com limite de tentativas: depois de
LOGIN_TENTATIVAS_MAXIMAS senhas erradas seguidas para o mesmo usuário ou IP,
novos logins são recusados por LOGIN_BLOQUEIO_MINUTOS desde a última falha
(mesmo com a senha certa).

O contador fica no cache do processo (um worker do uvicorn), então reiniciar o
app zera os bloqueios.
"""

import logging

from django.conf import settings
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.forms import AuthenticationForm
from django.core.cache import cache
from django.core.exceptions import ValidationError

from .auditoria import _ip

logger = logging.getLogger(__name__)


def _chaves(request, username) -> list[str]:
    chaves = [f'login-falhas:usuario:{str(username or "").strip().lower()}']
    ip = _ip(request) if request is not None else None
    if ip:
        chaves.append(f'login-falhas:ip:{ip}')
    return chaves


def bloqueado(request, username) -> bool:
    return any(
        cache.get(chave, 0) >= settings.LOGIN_TENTATIVAS_MAXIMAS
        for chave in _chaves(request, username)
    )


class BackendComLimiteDeTentativas(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        if bloqueado(request, username):
            logger.warning('Login bloqueado por excesso de tentativas: %s', username)
            return None
        usuario = super().authenticate(request, username=username, password=password, **kwargs)
        chaves = _chaves(request, username)
        if usuario is None:
            for chave in chaves:
                cache.set(chave, cache.get(chave, 0) + 1, timeout=settings.LOGIN_BLOQUEIO_MINUTOS * 60)
        else:
            cache.delete_many(chaves)
        return usuario


class FormularioLogin(AuthenticationForm):
    """Login do painel: avisa que está bloqueado em vez de "senha incorreta"."""

    bloqueado = False

    def clean(self):
        if bloqueado(self.request, self.cleaned_data.get('username')):
            self.bloqueado = True
            raise ValidationError(
                f'Muitas tentativas erradas. Aguarde {settings.LOGIN_BLOQUEIO_MINUTOS} minutos '
                'e tente de novo.',
                code='bloqueado',
            )
        return super().clean()
