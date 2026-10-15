import re
from decimal import Decimal

from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter
def brl(valor) -> str:
    """1234.5 -> 'R$ 1.234,50'."""
    if valor in (None, ''):
        return '—'
    valor = Decimal(str(valor))
    return f'R$ {valor:,.2f}'.replace(',', '_').replace('.', ',').replace('_', '.')


@register.filter
def telefone(numero: str) -> str:
    t = str(numero or '')
    if len(t) == 13 and t.startswith('55'):
        return f'({t[2:4]}) {t[4:9]}-{t[9:]}'
    if len(t) == 12 and t.startswith('55'):
        return f'({t[2:4]}) {t[4:8]}-{t[8:]}'
    return t


_FORMATOS_WHATSAPP = [
    (re.compile(r'\*([^*\n]+)\*'), r'<b>\1</b>'),
    (re.compile(r'(?<!\w)_([^_\n]+)_(?!\w)'), r'<i>\1</i>'),
    (re.compile(r'~([^~\n]+)~'), r'<s>\1</s>'),
    (re.compile(r'`([^`\n]+)`'), r'<code>\1</code>'),
]


@register.filter
def whatsapp(texto: str) -> str:
    """Escapa o HTML e aplica a formatação do WhatsApp (*negrito*, _itálico_...)."""
    html = escape(texto or '')
    for padrao, troca in _FORMATOS_WHATSAPP:
        html = padrao.sub(troca, html)
    return mark_safe(html.replace('\n', '<br>'))


@register.filter
def sem_formatacao(texto: str) -> str:
    """Remove marcações do WhatsApp (*, _, ~) e quebras de linha, para prévias."""
    for padrao, _ in _FORMATOS_WHATSAPP:
        texto = padrao.sub(r'\1', texto or '')
    return ' '.join(texto.split())


@register.filter
def get_item(dicionario: dict, chave):
    return dicionario.get(chave, chave)


@register.simple_tag(takes_context=True)
def url_com(context, **params) -> str:
    """Querystring atual com os parâmetros trocados (filtros/paginação)."""
    query = context['request'].GET.copy()
    for chave, valor in params.items():
        if valor in (None, ''):
            query.pop(chave, None)
        else:
            query[chave] = valor
    return f'?{query.urlencode()}' if query else '?'
