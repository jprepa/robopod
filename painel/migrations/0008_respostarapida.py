"""Respostas rápidas do chat do painel, já com as mensagens que antes eram
botões fixos na tela da conversa."""

from django.db import migrations, models

RESPOSTAS_INICIAIS = [
    ('Saudação', 'oi', 'Oi, {nome}! Aqui é da Robopod, já vou te ajudar 😊'),
    ('Chave PIX', 'pix', 'Chave PIX: {pix}'),
    ('Agradecimento', 'obrigado', 'Muito obrigado pela compra, {nome}! 💜 Qualquer coisa é só chamar aqui.'),
    ('Rastreio', 'rastreio', '🛵 Dá pra acompanhar a sua entrega por este link: '),
]


def criar_respostas(apps, schema_editor):
    RespostaRapida = apps.get_model('painel', 'RespostaRapida')
    if RespostaRapida.objects.exists():
        return
    for ordem, (titulo, atalho, texto) in enumerate(RESPOSTAS_INICIAIS):
        RespostaRapida.objects.create(titulo=titulo, atalho=atalho, texto=texto, ordem=ordem)


class Migration(migrations.Migration):

    dependencies = [
        ('painel', '0007_registroauditoria'),
    ]

    operations = [
        migrations.CreateModel(
            name='RespostaRapida',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('titulo', models.CharField(help_text='Texto do botão, ex.: Agradecimento', max_length=40)),
                ('atalho', models.SlugField(help_text='Comando digitado no chat, sem a barra. Ex.: obrigado → /obrigado', max_length=30, unique=True)),
                ('texto', models.TextField()),
                ('mostrar_botao', models.BooleanField(default=True, help_text='Desmarcado: só aparece digitando o comando')),
                ('ordem', models.PositiveIntegerField(default=0)),
                ('ativo', models.BooleanField(default=True)),
            ],
            options={
                'verbose_name': 'Resposta rápida',
                'verbose_name_plural': 'Respostas rápidas',
                'ordering': ['ordem', 'titulo'],
            },
        ),
        migrations.RunPython(criar_respostas, migrations.RunPython.noop),
    ]
