"""Grava no banco o cardápio que antes ficava fixo em core/llm_service.py.

Só roda se ainda não houver categorias; estoque começa vazio (não controla),
então o bot se comporta igual a antes até o estoque ser preenchido no painel.
"""

from decimal import Decimal

from django.db import migrations

from painel.catalogo_inicial import CATALOGO_INICIAL


def criar_cardapio(apps, schema_editor):
    Categoria = apps.get_model('painel', 'Categoria')
    Produto = apps.get_model('painel', 'Produto')
    Variante = apps.get_model('painel', 'Variante')
    if Categoria.objects.exists():
        return

    for ordem_cat, cat in enumerate(CATALOGO_INICIAL['categorias']):
        emoji, _, nome = cat['titulo'].partition(' ')
        categoria = Categoria.objects.create(slug=cat['id'], emoji=emoji, nome=nome, ordem=ordem_cat)
        for ordem_item, item in enumerate(cat['itens']):
            produto = Produto.objects.create(
                categoria=categoria,
                slug=item['id'],
                nome=item['nome'],
                descricao=item.get('descricao', ''),
                preco=Decimal(str(item['preco'])),
                imagem_url=item.get('imagem_url', ''),
                ordem=ordem_item,
            )
            for ordem_var, var in enumerate(item.get('variantes', [])):
                Variante.objects.create(
                    produto=produto,
                    slug=var['id'],
                    nome=var['nome'],
                    imagem_url=var.get('imagem_url', ''),
                    ordem=ordem_var,
                )


class Migration(migrations.Migration):

    dependencies = [
        ('painel', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(criar_cardapio, migrations.RunPython.noop),
    ]
