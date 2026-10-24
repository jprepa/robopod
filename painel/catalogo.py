"""Cardápio servido ao bot a partir do banco, com cache em memória.

O bot lê `atual()` (síncrono, sem consulta) e chama `garantir_atualizado()` no
início de cada mensagem. Alterações feitas no painel invalidam o cache na hora;
o TTL cobre alterações feitas por outro processo (ex.: shell, admin em outro worker).

Produtos/opções inativos ou com estoque zerado não aparecem para o cliente.
"""

import difflib
import re
import time
import unicodedata

from asgiref.sync import sync_to_async
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import Categoria, Produto, Variante

_TTL_SEGUNDOS = 60
# Semelhança mínima para casar o nome escrito pela IA com um produto
_SIMILARIDADE_MINIMA = 0.6
# Para o sabor é bem mais rígida: só cobre erro de digitação, nunca outro sabor
_SIMILARIDADE_SABOR = 0.85

_cache: dict = {'categorias': []}
_carregado_em = 0.0
_sujo = True


def atual() -> dict:
    """Cardápio no formato {'categorias': [{'id', 'titulo', 'itens': [...]}]}."""
    return _cache


def invalidar() -> None:
    global _sujo
    _sujo = True


@receiver([post_save, post_delete], sender=Categoria)
@receiver([post_save, post_delete], sender=Produto)
@receiver([post_save, post_delete], sender=Variante)
def _ao_alterar_catalogo(**kwargs):
    invalidar()


def _disponivel(estoque: int | None) -> bool:
    return estoque is None or estoque > 0


def _montar() -> dict:
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
                'preco': float(p.preco),
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


def recarregar() -> dict:
    global _cache, _carregado_em, _sujo
    _sujo = False
    _cache = _montar()
    _carregado_em = time.monotonic()
    return _cache


async def garantir_atualizado() -> dict:
    if _sujo or time.monotonic() - _carregado_em > _TTL_SEGUNDOS:
        await sync_to_async(recarregar)()
    return _cache


def atualizado() -> dict:
    """Versão síncrona de `garantir_atualizado()`, para as views do painel."""
    if _sujo or time.monotonic() - _carregado_em > _TTL_SEGUNDOS:
        recarregar()
    return _cache


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


def _validar_itens(itens: list[dict]) -> tuple[list[dict], list[str]]:
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
            'preco_unitario': float(produto.preco),  # preço do banco, não o da IA
            'produto_id': produto.pk,
            'variante_id': variante.pk if variante else None,
        })
    return validados, problemas


async def validar_itens(itens: list[dict]) -> tuple[list[dict], list[str]]:
    """Confere no banco os itens já casados com o cardápio (`conferir_itens` do
    bot), corrige preços e checa estoque.

    Returns:
        (itens validados, lista de problemas de estoque para avisar o cliente).
    """
    return await sync_to_async(_validar_itens)(itens)
