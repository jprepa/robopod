from django.db import migrations

GRUPO_OPERADOR = 'Operador'


def criar_grupo(apps, schema_editor):
    apps.get_model('auth', 'Group').objects.get_or_create(name=GRUPO_OPERADOR)


def remover_grupo(apps, schema_editor):
    apps.get_model('auth', 'Group').objects.filter(name=GRUPO_OPERADOR).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('auth', '0012_alter_user_first_name_max_length'),
        ('painel', '0016_mensagem_midia'),
    ]

    operations = [
        migrations.RunPython(criar_grupo, remover_grupo),
    ]
