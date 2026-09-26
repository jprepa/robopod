from django.contrib import admin
from .models import SessaoChat


@admin.register(SessaoChat)
class SessaoChatAdmin(admin.ModelAdmin):
    list_display = ('telefone', 'estado_atual', 'ultima_interacao')
    list_filter = ('estado_atual',)
    search_fields = ('telefone',)
    readonly_fields = ('criado_em', 'ultima_interacao')
