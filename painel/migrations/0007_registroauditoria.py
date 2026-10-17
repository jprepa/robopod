import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('painel', '0006_expediente'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='RegistroAuditoria',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('criado_em', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('responsavel', models.CharField(db_index=True, help_text='Usuário, ou "Bot"', max_length=150)),
                ('tipo', models.CharField(choices=[('acesso', 'Acesso'), ('conversa', 'Conversa'), ('cliente', 'Cliente'), ('pedido', 'Pedido'), ('estoque', 'Estoque'), ('configuracao', 'Configurações')], db_index=True, max_length=20)),
                ('acao', models.CharField(help_text='Identificador da ação, ex.: pedido_status', max_length=40)),
                ('descricao', models.CharField(max_length=255)),
                ('mudancas', models.JSONField(blank=True, default=dict, help_text='{campo: [antes, depois]}')),
                ('alvo_tipo', models.CharField(blank=True, max_length=20)),
                ('alvo_id', models.CharField(blank=True, max_length=40)),
                ('ip', models.GenericIPAddressField(blank=True, null=True)),
                ('usuario', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Registro de auditoria',
                'verbose_name_plural': 'Auditoria',
                'ordering': ['-criado_em', '-pk'],
                'indexes': [models.Index(fields=['alvo_tipo', 'alvo_id'], name='painel_auditoria_alvo_idx')],
            },
        ),
    ]
