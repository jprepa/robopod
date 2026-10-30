"""Cardápio servido ao bot a partir do banco, com cache em memória.

O bot lê `atual()` (síncrono, sem consulta) e chama `garantir_atualizado()` no
início de cada mensagem. Alterações feitas no painel invalidam o cache na hora;
o TTL cobre alterações feitas por outro processo (ex.: shell, admin em outro worker).

Cada loja tem o seu cardápio em cache: o mesmo catálogo e estoque, com os
preços próprios da loja (`PrecoLoja`). A loja da conversa vem de `usar_loja()`,
chamado no começo de cada resposta; vale só para a task que o chamou.

Produtos/opções inativos ou com estoque zerado não aparecem para o cliente.
"""

import difflib
import re
import time
import unicodedata
from contextvars import ContextVar

from asgiref.sync import sync_to_async
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import Categoria, PrecoLoja, Produto, Variante, precos_da_loja

_TTL_SEGUNDOS = 60
# Semelhança mínima para casar o nome escrito pela IA com um produto
_SIMILARIDADE_MINIMA = 0.6
# Para o sabor é bem mais rígida: só cobre erro de digitação, nunca outro sabor
_SIMILARIDADE_SABOR = 0.85

_VAZIO: dict = {'categorias': []}
# loja_id -> (versão, carregado_em, cardápio). Invalidar só sobe a versão: até
# recarregar, `atual()` segue devolvendo o cardápio anterior (nunca vazio).
_caches: dict[int | None, tuple[int, float, dict]] = {}
_versao = 0
_loja: ContextVar[int | None] = ContextVar('catalogo_loja', default=None)


def usar_loja(loja_id: int | None) -> None:
    """Loja da conversa atendida nesta task: define os preços de `atual()`."""
    _loja.set(loja_id)


def atual() -> dict:
    """Cardápio no formato {'categorias': [{'id', 'titulo', 'itens': [...]}]}."""
    entrada = _caches.get(_loja.get())
    return entrada[2] if entrada else _VAZIO


def invalidar() -> None:
    global _versao
    _versao += 1


@receiver([post_save, post_delete], sender=Categoria)
@receiver([post_save, post_delete], sender=Produto)
@receiver([post_save, post_delete], sender=Variante)
@receiver([post_save, post_delete], sender=PrecoLoja)
def _ao_alterar_catalogo(**kwargs):
    invalidar()


def _disponivel(estoque: int | None) -> bool:
    return estoque is None or estoque > 0


def _montar(loja_id: int | None) -> dict:
    precos = precos_da_loja(loja_id)
    categorias = []
    for cat in Categoria.objects.filter(ativo=True).prefetch_related('produtos__variantes'):
        itens = []
        for p in cat.produtos.all():
            if not p.ativo:
                continue
            todas = [v for v in p.variantes.all() if v.ativo]
            variantes = [v for v in todas if _disponivel(v.estoque)]
            if todas and not variantes:
                continue  # todas as opções esgotadas
            if not todas and not _disponivel(p.estoque):
                continue
            itens.append({
                'id': p.slug,
                'nome': p.nome,
                'preco': float(precos.get(p.pk, p.preco)),
                'descricao': p.descricao,
                'imagem_url': p.imagem_url,
                'variantes': [
                    {'id': v.slug, 'nome': v.nome, 'imagem_url': v.imagem_url}
                    for v in variantes
                ],
            })
        if itens:
            categorias.append({
                'id': cat.slug, 'emoji': cat.emoji, 'nome': cat.nome,
                'titulo': cat.titulo, 'itens': itens,
            })
    return {'categorias': categorias}


def recarregar(loja_id: int | None = None) -> dict:
    versao = _versao
    cardapio = _montar(loja_id)
    _caches[loja_id] = (versao, time.monotonic(), cardapio)
    return cardapio


def _vencido(loja_id: int | None) -> bool:
    entrada = _caches.get(loja_id)
    return entrada is None or entrada[0] != _versao or time.monotonic() - entrada[1] > _TTL_SEGUNDOS


async def garantir_atualizado() -> dict:
    """Recarrega, se preciso, o cardápio da loja de `usar_loja()`."""
    loja_id = _loja.get()
    if _vencido(loja_id):
        return await sync_to_async(recarregar)(loja_id)
    return _caches[loja_id][2]


def atualizado(loja_id: int | None = None) -> dict:
    """Versão síncrona de `garantir_atualizado()`, para as views do painel."""
    if _vencido(loja_id):
        return recarregar(loja_id)
    return _caches[loja_id][2]


# ── Validação do carrinho fechado pela IA ──


def _normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize('NFKD', texto).encode('ascii', 'ignore').decode()
    return re.sub(r'\s+', ' ', re.sub(r'[^\w\s-]', ' ', sem_acento.lower())).strip()


def _sem_parenteses(texto: str) -> str:
    return re.sub(r'\([^)]*\)', ' ', texto)


def _similaridade(a: str, b: str) -> float:
    pares = [
        (_normalizar(a), _normalizar(b)),
        (_normalizar(_sem_parenteses(a)), _normalizar(_sem_parenteses(b))),
    ]
    return max(difflib.SequenceMatcher(None, x, y).ratio() for x, y in pares)


def localizar_item(referencia: str, itens: list[dict]) -> dict | None:
    """Acha o item do cardápio (dicts de `atual()`) mais parecido com o texto.

    Aceita o nome completo ou só parte dele ("V35", "elfbar 15k"): quando a
    referência aparece inteira no nome do item, ele tem prioridade.
    """
    alvo = _normalizar(referencia)
    if not alvo:
        return None
    contem = [
        i for i in itens
        if re.search(rf'(?<!\w){re.escape(alvo)}(?!\w)', _normalizar(i['nome']))
    ]
    candidatos, minimo = (contem, 0.0) if contem else (itens, _SIMILARIDADE_MINIMA)
    melhor, melhor_nota = None, minimo
    for item in candidatos:
        nota = _similaridade(referencia, item['nome'])
        if nota >= melhor_nota:
            melhor, melhor_nota = item, nota
    return melhor


def _contem(trecho: str, texto: str) -> bool:
    """`trecho` aparece inteiro (palavras completas) em `texto`, já normalizados."""
    return bool(trecho) and re.search(rf'(?<!\w){re.escape(trecho)}(?!\w)', texto) is not None


def localizar_variante(referencia: str, variantes: list[dict]) -> dict | None:
    """Acha o sabor/opção (dicts de `atual()`) que a IA escolheu.

    Aceita o nome exato (sem diferença de acento, emoji ou maiúsculas), parte
    dele quando só um sabor combina ("watermelon" -> "Strawberry Apple
    Watermelon"), o nome com o modelo junto ou um erro de digitação leve.
    Na dúvida devolve None: o cliente escolhe na lista em vez de o bot chutar.
    """
    alvo = _normalizar(referencia)
    if not alvo:
        return None
    nomes = [_normalizar(v['nome']) for v in variantes]
    for variante, nome in zip(variantes, nomes):
        if nome == alvo:
            return variante

    contem = [v for v, nome in zip(variantes, nomes) if _contem(alvo, nome)]
    if contem:
        return contem[0] if len(contem) == 1 else None

    # "Ignite V35 - Cherry Ice": o sabor veio junto com o modelo
    dentro = [(len(nome), v) for v, nome in zip(variantes, nomes) if _contem(nome, alvo)]
    if dentro:
        return max(dentro, key=lambda par: par[0])[1]

    notas = sorted(
        ((difflib.SequenceMatcher(None, alvo, nome).ratio(), i) for i, nome in enumerate(nomes)),
        reverse=True,
    )
    if notas and notas[0][0] >= _SIMILARIDADE_SABOR and (
        len(notas) == 1 or notas[1][0] < notas[0][0]
    ):
        return variantes[notas[0][1]]
    return None


def _validar_itens(itens: list[dict], loja_id: int | None) -> tuple[list[dict], list[str]]:
    precos = precos_da_loja(loja_id)
    validados, problemas = [], []
    for item in itens:
        quantidade = max(1, int(item.get('quantidade') or 1))
        produto = Produto.objects.filter(
            slug=item.get('produto_slug'), ativo=True, categoria__ativo=True,
        ).first()
        variante = None
        if produto is not None and item.get('variante_slug'):
            variante = produto.variantes.filter(slug=item['variante_slug'], ativo=True).first()
        if produto is None or (item.get('variante_slug') and variante is None):
            problemas.append(f"{item.get('nome', 'um dos itens')} não está mais disponível")
            continue

        nome = f'{produto.nome} - {variante.nome}' if variante else produto.nome
        estoque = variante.estoque if variante else produto.estoque
        if estoque is not None and quantidade > estoque:
            problemas.append(
                f'{nome} está esgotado' if estoque <= 0
                else f'só temos {estoque} un. de {nome}',
            )
        validados.append({
            'nome': nome,
            'quantidade': quantidade,
            'preco_unitario': float(precos.get(produto.pk, produto.preco)),  # do banco, não o da IA
            'produto_id': produto.pk,
            'variante_id': variante.pk if variante else None,
        })
    return validados, problemas


async def validar_itens(itens: list[dict]) -> tuple[list[dict], list[str]]:
    """Confere no banco os itens já casados com o cardápio (`conferir_itens` do
    bot), corrige preços (os da loja de `usar_loja()`) e checa estoque.

    Returns:
        (itens validados, lista de problemas de estoque para avisar o cliente).
    """
    return await sync_to_async(_validar_itens)(itens, _loja.get())
