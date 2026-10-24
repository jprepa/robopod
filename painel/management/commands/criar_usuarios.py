"""Cria os logins do painel para a equipe, com senha aleatória (mostrada uma única vez).

    python manage.py criar_usuarios                 # cria os que ainda não existem
    python manage.py criar_usuarios --redefinir     # gera nova senha para os que já existem
    python manage.py criar_usuarios --superusuario  # também libera a aba Auditoria

Usuário de acesso: nome.sobrenome em minúsculas (ex.: gustavo.beozzo).
"""

import secrets

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

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

    def handle(self, *args, **opts):
        User = get_user_model()
        credenciais = []
        for nome, sobrenome in PESSOAS:
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
            credenciais.append((login, senha))

        if credenciais:
            self.stdout.write(self.style.SUCCESS('\nLogins criados (anote as senhas, não serão mostradas de novo):'))
            for login, senha in credenciais:
                self.stdout.write(f'  usuário: {login}   senha: {senha}')
