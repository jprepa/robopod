"""Cadastra o número que já estava em uso (EVOLUTION_INSTANCE) como a primeira
loja, principal, e liga a ela os clientes, pedidos e mensagens que já existem."""

from django.conf import settings
from django.db import migrations


def criar_loja_inicial(apps, schema_editor):
    Loja = apps.get_model('painel', 'Loja')
    Cliente = apps.get_model('painel', 'Cliente')
    Pedido = apps.get_model('painel', 'Pedido')
    Mensagem = apps.get_model('painel', 'Mensagem')
    if Loja.objects.exists():
        return
    loja = Loja.objects.create(nome='Loja 1', instancia=settings.EVOLUTION_INSTANCE, principal=True)
    Cliente.objects.update(loja_origem=loja, loja_atual=loja)
    Pedido.objects.filter(origem='whatsapp').update(loja=loja)
    Mensagem.objects.update(loja=loja)


class Migration(migrations.Migration):

    dependencies = [
        ('painel', '0013_lojas'),
    ]

    operations = [
        migrations.RunPython(criar_loja_inicial, migrations.RunPython.noop),
    ]
