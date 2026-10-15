from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

app_name = 'painel'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('entrar/', auth_views.LoginView.as_view(
        template_name='painel/login.html', redirect_authenticated_user=True,
    ), name='login'),
    path('sair/', auth_views.LogoutView.as_view(), name='logout'),
    path('contadores/', views.contadores, name='contadores'),

    path('conversas/', views.conversas, name='conversas'),
    path('conversas/<str:telefone>/', views.conversa, name='conversa'),
    path('conversas/<str:telefone>/responder/', views.conversa_responder, name='conversa_responder'),
    path('conversas/<str:telefone>/assumir/', views.conversa_assumir, name='conversa_assumir'),
    path('conversas/<str:telefone>/devolver/', views.conversa_devolver, name='conversa_devolver'),

    path('clientes/', views.clientes, name='clientes'),
    path('clientes/<int:pk>/', views.cliente, name='cliente'),

    path('pedidos/', views.pedidos, name='pedidos'),
    path('pedidos/venda-manual/', views.venda_manual, name='venda_manual'),
    path('pedidos/<int:pk>/', views.pedido, name='pedido'),
    path('pedidos/<int:pk>/status/', views.pedido_status, name='pedido_status'),

    path('estoque/', views.estoque, name='estoque'),
    path('estoque/<str:tipo>/<int:pk>/ajustar/', views.estoque_ajustar, name='estoque_ajustar'),
    path('estoque/produto/novo/', views.produto_editar, name='produto_novo'),
    path('estoque/produto/<int:pk>/', views.produto_editar, name='produto_editar'),
    path('estoque/categorias/', views.categorias, name='categorias'),

    path('configuracoes/', views.configuracoes, name='configuracoes'),
]
