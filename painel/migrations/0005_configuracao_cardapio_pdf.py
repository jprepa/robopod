from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('painel', '0004_venda_manual'),
    ]

    operations = [
        migrations.AddField(
            model_name='configuracao',
            name='cardapio_pdf',
            field=models.FileField(blank=True, help_text='PDF do cardápio enviado ao cliente junto das boas-vindas', upload_to='cardapio/'),
        ),
    ]
