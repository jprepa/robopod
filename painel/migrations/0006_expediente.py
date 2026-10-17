from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('painel', '0005_configuracao_cardapio_pdf'),
    ]

    operations = [
        migrations.AddField(
            model_name='configuracao',
            name='expediente_aberto',
            field=models.BooleanField(default=True, help_text='Fechado: o bot só responde com a mensagem de loja fechada'),
        ),
        migrations.AddField(
            model_name='configuracao',
            name='expediente_aberto_em',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='configuracao',
            name='expediente_fechado_em',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='configuracao',
            name='horario_atendimento',
            field=models.CharField(blank=True, default='Todos os dias, das 18h às 03h', max_length=120),
        ),
        migrations.AddField(
            model_name='configuracao',
            name='mensagem_fechado',
            field=models.TextField(blank=True, default=(
                '🌙 Oi! No momento a *Robopod* está fechada e o atendimento está pausado.\n'
                'Assim que abrirmos, é só mandar mensagem que a gente te atende 😉'
            )),
        ),
    ]
