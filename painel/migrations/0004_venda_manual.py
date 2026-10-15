import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('painel', '0003_configuracao'),
    ]

    operations = [
        migrations.AlterField(
            model_name='pedido',
            name='cliente',
            field=models.ForeignKey(blank=True, help_text='Vazio em vendas manuais sem cliente identificado (balcão)', null=True, on_delete=django.db.models.deletion.PROTECT, related_name='pedidos', to='painel.cliente'),
        ),
        migrations.AddField(
            model_name='pedido',
            name='origem',
            field=models.CharField(choices=[('whatsapp', 'WhatsApp'), ('manual', 'Venda manual')], default='whatsapp', max_length=10),
        ),
    ]
