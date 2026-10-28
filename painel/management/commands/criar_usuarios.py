"""Cria os logins do painel para a equipe, com senha aleatória (mostrada uma única vez).

    python manage.py criar_usuarios                 # cria os que ainda não existem
    python manage.py criar_usuarios --redefinir     # gera nova senha para os que já existem
    python manage.py criar_usuarios --superusuario  # também libera a aba Auditoria
    python manage.py criar_usuarios --operador Maria Silva  # cria um Operador (Conversas, Estoque e Configurações)

Usuário de acesso: nome.sobrenome em minúsculas (ex.: gustavo.beozzo).
"""

import secrets

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError

from painel.permissoes import GRUPO_OPERADOR

PESSOAS = [
    ('Gustavo', 'Beozzo'),
    ('Andre', 'Beozzo'),
    ('Tiago', 'Beozzo'),
]


class Command(BaseCommand):
    help = 'Cria os logins do painel (Gustavo, Andre e Tiago Beozzo) com senha aleatória.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--redefinir', action='store_true',
            help='Gera uma nova senha para quem já existe',
        )
        parser.add_argument(
            '--superusuario', action='store_true',
            help='Cria como superusuário (acessa Auditoria e o /admin)',
        )
        parser.add_argument(
            '--operador', nargs=2, metavar=('NOME', 'SOBRENOME'),
            help='Cria só esta pessoa, no perfil Operador, em vez da equipe',
        )

    def handle(self, *args, **opts):
        User = get_user_model()
        credenciais = []
        if opts['operador'] and opts['superusuario']:
            raise CommandError('Use --operador ou --superusuario, não os dois.')
        pessoas = [tuple(opts['operador'])] if opts['operador'] else PESSOAS
        for nome, sobrenome in pessoas:
            login = f'{nome}.{sobrenome}'.lower()
            usuario = User.objects.filter(username=login).first()
            if usuario and not opts['redefinir']:
                self.stdout.write(f'{login}: já existe (use --redefinir para gerar nova senha)')
                continue
            senha = secrets.token_urlsafe(9)
            if not usuario:
                usuario = User(username=login)
            usuario.first_name, usuario.last_name = nome, sobrenome
            if opts['superusuario']:
                usuario.is_staff = usuario.is_superuser = True
            usuario.set_password(senha)
            usuario.save()
            if opts['operador']:
                usuario.groups.add(Group.objects.get_or_create(name=GRUPO_OPERADOR)[0])
            credenciais.append((login, senha))

        if credenciais:
            self.stdout.write(self.style.SUCCESS('\nLogins criados (anote as senhas, não serão mostradas de novo):'))
            for login, senha in credenciais:
                self.stdout.write(f'  usuário: {login}   senha: {senha}')
