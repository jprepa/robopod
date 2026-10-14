from django.contrib import admin

from .models import (
    Categoria, Cliente, Configuracao, ItemPedido, Mensagem, Pedido, Produto, Variante,
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
    list_display = ('id', 'cliente', 'status', 'total', 'criado_em')
    list_filter = ('status',)
    search_fields = ('cliente__telefone', 'cliente__nome')
    inlines = [ItemPedidoInline]


@admin.register(Mensagem)
class MensagemAdmin(admin.ModelAdmin):
    list_display = ('telefone', 'autor', 'texto', 'criado_em')
    list_filter = ('autor',)
    search_fields = ('telefone', 'texto')
