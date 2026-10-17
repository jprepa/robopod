import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def preencher_vendedor(apps, schema_editor):
    """Vendas manuais antigas: o vendedor é quem lançou, segundo a auditoria."""
    Pedido = apps.get_model('painel', 'Pedido')
    RegistroAuditoria = apps.get_model('painel', 'RegistroAuditoria')
    registros = RegistroAuditoria.objects.filter(
        acao='venda_manual', alvo_tipo='pedido', usuario__isnull=False,
    ).values_list('alvo_id', 'usuario_id')
    for alvo_id, usuario_id in registros:
        if alvo_id.isdigit():
            Pedido.objects.filter(pk=int(alvo_id), origem='manual').update(vendedor_id=usuario_id)


class Migration(migrations.Migration):

    dependencies = [
        ('painel', '0008_respostarapida'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='configuracao',
            name='bot_pausado',
            field=models.BooleanField(default=False, help_text='Pausado: o bot não responde ninguém (nem com a mensagem de loja fechada)'),
        ),
        migrations.AddField(
            model_name='configuracao',
            name='bot_pausado_em',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='pedido',
            name='vendedor',
            field=models.ForeignKey(blank=True, help_text='Quem lançou a venda manual. Vazio = pedido feito pelo bot no WhatsApp', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.RunPython(preencher_vendedor, migrations.RunPython.noop),
    ]
