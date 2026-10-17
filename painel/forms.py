import re

from django import forms
from django.forms import inlineformset_factory

from .models import Categoria, Cliente, Produto, RespostaRapida, Variante

# Comandos do chat que já vêm prontos (ver painel/views.py: _comandos_chat)
COMANDOS_FIXOS = ('nome', 'endereco', 'pedido')


class ClienteForm(forms.ModelForm):
    class Meta:
        model = Cliente
        fields = ['nome', 'observacoes', 'bloqueado']
        widgets = {'observacoes': forms.Textarea(attrs={'rows': 3})}


class CategoriaForm(forms.ModelForm):
    class Meta:
        model = Categoria
        fields = ['emoji', 'nome', 'ordem', 'ativo']


class ProdutoForm(forms.ModelForm):
    class Meta:
        model = Produto
        fields = [
            'categoria', 'nome', 'descricao', 'preco', 'estoque', 'estoque_minimo',
            'imagem_url', 'ordem', 'ativo',
        ]


VarianteFormSet = inlineformset_factory(
    Produto, Variante,
    fields=['nome', 'estoque', 'imagem_url', 'ordem', 'ativo'],
    extra=2, can_delete=True,
)


class RespostaRapidaForm(forms.ModelForm):
    # Aceita "/obrigado" ou "Obrigado": guarda só "obrigado"
    atalho = forms.CharField(max_length=31)

    class Meta:
        model = RespostaRapida
        fields = ['titulo', 'atalho', 'texto', 'mostrar_botao', 'ordem', 'ativo']
        widgets = {'texto': forms.Textarea(attrs={'rows': 4})}

    def clean_atalho(self):
        atalho = self.cleaned_data['atalho'].strip().lstrip('/').lower()
        if not re.fullmatch(r'[a-z0-9_-]{1,30}', atalho):
            raise forms.ValidationError('Use só letras sem acento, números, - ou _ (sem espaços).')
        if atalho in COMANDOS_FIXOS:
            raise forms.ValidationError(f'/{atalho} já é um comando do painel. Escolha outro.')
        return atalho
