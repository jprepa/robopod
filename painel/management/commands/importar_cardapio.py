"""Atualiza o cardápio do bot a partir de um arquivo de texto (padrão: cardapio.txt).

    python manage.py importar_cardapio                  # aplica cardapio.txt
    python manage.py importar_cardapio --simular        # só mostra o que mudaria
    python manage.py importar_cardapio outro.txt --manter-outros

Formato (o mesmo da lista de sabores do WhatsApp), modelos separados por linha em branco:

    Ignite V35 - 3.500 Puffs
    R$ 90,00

    Cherry ice 🍒🧊
    ❌Menthol 🌿            <- ❌ = esgotado (estoque 0, some do cardápio)

A primeira palavra do modelo é a marca, que vira a categoria. Sabor repetido em
modelos da mesma marca usa a grafia e os emojis da primeira vez que aparece.

Produtos e sabores são atualizados pelo nome, sem duplicar. O que não está no
arquivo é ocultado (não apagado); use --manter-outros para não mexer nos demais
produtos. Sabor sem ❌ que estava zerado volta a ficar disponível.
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

from painel.models import Categoria, Produto, Variante

_EMOJI_CATEGORIA = '💨'
_ESGOTADO = '❌'
_RE_MODELO = re.compile(r'^(?P<nome>.+?)\s*-\s*(?P<puffs>[\d.]+)\s*puffs\.?$', re.IGNORECASE)
_RE_PRECO = re.compile(r'^R\$\s*(?P<valor>[\d.]+(?:,\d{1,2})?)$')
# Caracteres que fazem parte do nome do sabor; o resto (emojis) fica à parte
_CARACTERES_NOME = set(" -'&./()")


class _Simulacao(Exception):
    """Desfaz a transação no modo --simular."""


@dataclass
class Sabor:
    nome: str
    emojis: str
    esgotado: bool

    @property
    def chave(self) -> str:
        return _chave(self.nome)

    @property
    def titulo(self) -> str:
        return f'{self.nome} {self.emojis}'.strip()


@dataclass
class Modelo:
    nome: str
    puffs: str
    preco: Decimal | None = None
    sabores: list[Sabor] = field(default_factory=list)

    @property
    def marca(self) -> str:
        return self.nome.split()[0]

    @property
    def nome_produto(self) -> str:
        return f'{self.nome} ({self.puffs} puffs)'


def _chave(nome: str) -> str:
    return re.sub(r'\s+', ' ', nome).strip().lower()


def _maiusculas(texto: str) -> str:
    """'Strawberry ice' -> 'Strawberry Ice', sem baixar letras já maiúsculas."""
    return ' '.join(p[:1].upper() + p[1:] for p in texto.split())


def _separar_emojis(texto: str) -> tuple[str, str]:
    for i, c in enumerate(texto):
        if not (c.isalnum() or c in _CARACTERES_NOME):
            return texto[:i].strip(), re.sub(r'\s+', '', texto[i:])
    return texto.strip(), ''


def _ler(caminho: Path) -> list[Modelo]:
    modelos: list[Modelo] = []
    for n, linha in enumerate(caminho.read_text(encoding='utf-8').splitlines(), start=1):
        linha = linha.replace(' ', ' ').strip()
        if not linha:
            continue
        if m := _RE_MODELO.match(linha):
            modelos.append(Modelo(nome=re.sub(r'\s+', ' ', m['nome']), puffs=m['puffs']))
            continue
        if not modelos:
            raise CommandError(f'Linha {n}: esperava o modelo ("Nome - 1.000 puffs"): {linha!r}')
        modelo = modelos[-1]
        if m := _RE_PRECO.match(linha):
            valor = m['valor'].replace('.', '').replace(',', '.')
            modelo.preco = Decimal(valor).quantize(Decimal('0.01'))
            continue
        esgotado = linha.startswith(_ESGOTADO)
        nome, emojis = _separar_emojis(linha.removeprefix(_ESGOTADO).strip())
        if not nome:
            raise CommandError(f'Linha {n}: sabor sem nome: {linha!r}')
        modelo.sabores.append(Sabor(nome=_maiusculas(nome), emojis=emojis, esgotado=esgotado))

    for modelo in modelos:
        if modelo.preco is None:
            raise CommandError(f'{modelo.nome}: faltou o preço (ex.: "R$ 90,00").')
        if not modelo.sabores:
            raise CommandError(f'{modelo.nome}: nenhum sabor listado.')
    _padronizar_por_marca(modelos)
    return modelos


def _padronizar_por_marca(modelos: list[Modelo]) -> None:
    """Sabor repetido na mesma marca fica com a grafia/emojis da 1ª ocorrência."""
    primeiros: dict[tuple[str, str], Sabor] = {}
    for modelo in modelos:
        for sabor in modelo.sabores:
            ref = primeiros.setdefault((modelo.marca.lower(), sabor.chave), sabor)
            sabor.nome, sabor.emojis = ref.nome, ref.emojis


class Command(BaseCommand):
    help = 'Atualiza categorias, produtos, preços e sabores a partir de um arquivo de texto.'

    def add_arguments(self, parser):
        parser.add_argument(
            'arquivo', nargs='?', default=str(Path(settings.BASE_DIR) / 'cardapio.txt'),
            help='Arquivo do cardápio (padrão: cardapio.txt na raiz do projeto)',
        )
        parser.add_argument(
            '--simular', action='store_true', help='Mostra o que mudaria, sem gravar',
        )
        parser.add_argument(
            '--manter-outros', action='store_true',
            help='Não oculta produtos/categorias que não estão no arquivo',
        )

    def handle(self, *args, arquivo, simular=False, manter_outros=False, **options):
        caminho = Path(arquivo)
        if not caminho.is_file():
            raise CommandError(f'Arquivo não encontrado: {caminho}')
        modelos = _ler(caminho)

        try:
            with transaction.atomic():
                self._aplicar(modelos, manter_outros)
                if simular:
                    raise _Simulacao
        except _Simulacao:
            self.stdout.write(self.style.WARNING('Simulação: nada foi gravado.'))
            return
        self.stdout.write(self.style.SUCCESS('Cardápio atualizado.'))

    def _aplicar(self, modelos: list[Modelo], manter_outros: bool) -> None:
        marcas = list(dict.fromkeys(m.marca for m in modelos))
        categorias = {marca: self._categoria(marca, ordem) for ordem, marca in enumerate(marcas)}

        produtos_ids = []
        for ordem, modelo in enumerate(modelos):
            produto = self._produto(modelo, categorias[modelo.marca], ordem)
            produtos_ids.append(produto.pk)
            self._sabores(produto, modelo.sabores)

        if manter_outros:
            return
        for produto in Produto.objects.filter(ativo=True).exclude(pk__in=produtos_ids):
            produto.ativo = False
            produto.save(update_fields=['ativo'])
            self.stdout.write(f'  ocultado: {produto.nome}')
        cat_ids = [c.pk for c in categorias.values()]
        for categoria in Categoria.objects.filter(ativo=True).exclude(pk__in=cat_ids):
            categoria.ativo = False
            categoria.save(update_fields=['ativo'])
            self.stdout.write(f'  categoria ocultada: {categoria.titulo}')

    def _categoria(self, marca: str, ordem: int) -> Categoria:
        categoria = Categoria.objects.filter(slug=slugify(marca)).first()
        if categoria is None:
            categoria = Categoria(slug=slugify(marca), emoji=_EMOJI_CATEGORIA, nome=marca)
            self.stdout.write(f'Nova categoria: {_EMOJI_CATEGORIA} {marca}')
        categoria.ordem, categoria.ativo = ordem, True
        categoria.save()
        return categoria

    def _produto(self, modelo: Modelo, categoria: Categoria, ordem: int) -> Produto:
        slug = slugify(modelo.nome)[:60]
        produto = Produto.objects.filter(slug=slug).first()
        if produto is None:
            produto = Produto(slug=slug)
            self.stdout.write(f'Novo produto: {modelo.nome_produto} — R$ {modelo.preco}')
        elif produto.preco != modelo.preco:
            self.stdout.write(f'Preço: {produto.nome} R$ {produto.preco} -> R$ {modelo.preco}')
        produto.categoria = categoria
        produto.nome = modelo.nome_produto
        produto.descricao = f'{modelo.puffs} puffs'
        produto.preco = modelo.preco
        produto.estoque = None  # o estoque fica em cada sabor
        produto.ativo = True
        produto.ordem = ordem
        produto.save()
        return produto

    def _sabores(self, produto: Produto, sabores: list[Sabor]) -> None:
        existentes = {_chave(_separar_emojis(v.nome)[0]): v for v in produto.variantes.all()}
        vistos = set()
        for ordem, sabor in enumerate(sabores):
            if sabor.chave in vistos:
                continue  # sabor repetido no mesmo modelo
            vistos.add(sabor.chave)
            variante = existentes.get(sabor.chave) or Variante(produto=produto)
            if sabor.esgotado:
                variante.estoque = 0
            elif variante.estoque == 0:
                variante.estoque = None  # voltou: deixa de controlar até contarem no painel
            variante.nome = sabor.titulo
            variante.ativo = True
            variante.ordem = ordem
            variante.save()

        for chave, variante in existentes.items():
            if chave not in vistos and variante.ativo:
                variante.ativo = False
                variante.save(update_fields=['ativo'])
                self.stdout.write(f'  sabor ocultado: {produto.nome} - {variante.nome}')

        disponiveis = sum(1 for s in sabores if not s.esgotado)
        self.stdout.write(f'  {produto.nome}: {disponiveis} sabores disponíveis, '
                          f'{len(vistos) - disponiveis} esgotados')
