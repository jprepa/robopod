from django import forms
from django.forms import inlineformset_factory

from .models import Categoria, Cliente, Produto, Variante


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
