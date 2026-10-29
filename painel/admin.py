from django.contrib import admin

from .models import (
    Categoria, Cliente, Configuracao, InscricaoPush, ItemPedido, Mensagem, Pedido, PerfilEquipe, Produto,
    RegistroAuditoria, Revendedor, Variante,
)

admin.site.register(Configuracao)


@admin.register(Cliente)
class ClienteAdmin(admin.ModelAdmin):
    list_display = ('telefone', 'nome', 'nome_whatsapp', 'bloqueado', 'ultima_mensagem_em')
    list_filter = ('bloqueado',)
    search_fields = ('telefone', 'nome', 'nome_whatsapp')


@admin.register(Categoria)
class CategoriaAdmin(admin.ModelAdmin):
    list_display = ('titulo', 'ordem', 'ativo')


class VarianteInline(admin.TabularInline):
    model = Variante
    extra = 0


@admin.register(Produto)
class ProdutoAdmin(admin.ModelAdmin):
    list_display = ('nome', 'categoria', 'preco', 'estoque', 'ativo')
    list_filter = ('categoria', 'ativo')
    search_fields = ('nome',)
    inlines = [VarianteInline]


class ItemPedidoInline(admin.TabularInline):
    model = ItemPedido
    extra = 0


@admin.register(Pedido)
class PedidoAdmin(admin.ModelAdmin):
    list_display = ('id', 'cliente', 'revendedor', 'origem', 'status', 'total', 'criado_em')
    list_filter = ('status', 'origem')
    search_fields = ('cliente__telefone', 'cliente__nome')
    inlines = [ItemPedidoInline]


@admin.register(Revendedor)
class RevendedorAdmin(admin.ModelAdmin):
    list_display = ('nome', 'telefone', 'ativo')
    list_filter = ('ativo',)
    search_fields = ('nome', 'telefone')


@admin.register(PerfilEquipe)
class PerfilEquipeAdmin(admin.ModelAdmin):
    list_display = ('usuario', 'whatsapp')
    search_fields = ('usuario__username', 'whatsapp')


@admin.register(Mensagem)
class MensagemAdmin(admin.ModelAdmin):
    list_display = ('telefone', 'autor', 'texto', 'criado_em')
    list_filter = ('autor',)
    search_fields = ('telefone', 'texto')


@admin.register(RegistroAuditoria)
class RegistroAuditoriaAdmin(admin.ModelAdmin):
    """Só leitura: registro de auditoria não se edita nem se apaga."""
    list_display = ('criado_em', 'responsavel', 'tipo', 'descricao', 'ip')
    list_filter = ('tipo', 'responsavel')
    search_fields = ('descricao', 'responsavel', 'alvo_id')
    date_hierarchy = 'criado_em'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(InscricaoPush)
class InscricaoPushAdmin(admin.ModelAdmin):
    list_display = ('usuario', 'aparelho', 'criado_em', 'ultimo_envio')
    list_filter = ('aparelho',)
    readonly_fields = ('endpoint', 'p256dh', 'auth', 'criado_em', 'ultimo_envio')
