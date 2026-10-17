import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

_FATURADOS = ('pago', 'em_entrega', 'entregue')
_ROTULOS_FATURADOS = ('Pago', 'Saiu para entrega', 'Entregue')


def preencher_aprovador(apps, schema_editor):
    """Pedidos do WhatsApp já faturados: o vendedor é quem aprovou o pagamento,
    segundo a auditoria (primeira mudança de status para pago/entrega/entregue)."""
    Pedido = apps.get_model('painel', 'Pedido')
    RegistroAuditoria = apps.get_model('painel', 'RegistroAuditoria')
    pendentes = set(
        Pedido.objects.filter(origem='whatsapp', status__in=_FATURADOS, vendedor__isnull=True)
        .values_list('pk', flat=True)
    )
    if not pendentes:
        return
    registros = RegistroAuditoria.objects.filter(
        acao='pedido_status', alvo_tipo='pedido', usuario__isnull=False,
    ).order_by('criado_em').values_list('alvo_id', 'usuario_id', 'mudancas')
    for alvo_id, usuario_id, mudancas in registros:
        if not alvo_id.isdigit() or int(alvo_id) not in pendentes:
            continue
        antes, depois = (mudancas or {}).get('Status', [None, None])
        if depois in _ROTULOS_FATURADOS and antes not in _ROTULOS_FATURADOS:
            Pedido.objects.filter(pk=int(alvo_id)).update(vendedor_id=usuario_id)
            pendentes.discard(int(alvo_id))


class Migration(migrations.Migration):

    dependencies = [
        ('painel', '0009_bot_pausado_vendedor'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name='pedido',
            name='vendedor',
            field=models.ForeignKey(blank=True, help_text='Quem lançou a venda manual ou aprovou o pagamento do pedido do WhatsApp', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.RunPython(preencher_aprovador, migrations.RunPython.noop),
    ]
