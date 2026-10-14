"""Dados da loja exibidos no painel: clientes, catálogo/estoque, pedidos e
o histórico de mensagens do WhatsApp."""

from decimal import Decimal

from django.db import models
from django.utils import timezone
from django.utils.text import slugify


class Cliente(models.Model):
    """Cliente identificado pelo número do WhatsApp."""

    telefone = models.CharField(max_length=20, unique=True, help_text='Formato 55XXXXXXXXXXX')
    nome = models.CharField(max_length=120, blank=True)
    nome_whatsapp = models.CharField(max_length=120, blank=True, help_text='Nome do perfil no WhatsApp')
    observacoes = models.TextField(blank=True)
    ultimo_endereco = models.CharField(max_length=255, blank=True)
    bloqueado = models.BooleanField(default=False, help_text='O bot ignora mensagens deste número')
    conversa_lida_em = models.DateTimeField(null=True, blank=True)
    ultima_mensagem_em = models.DateTimeField(null=True, blank=True, db_index=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-ultima_mensagem_em']

    def __str__(self):
        return f'{self.nome_exibicao} ({self.telefone})'

    @property
    def nome_exibicao(self) -> str:
        return self.nome or self.nome_whatsapp or self.telefone_formatado

    @property
    def telefone_formatado(self) -> str:
        t = self.telefone
        if len(t) == 13 and t.startswith('55'):
            return f'({t[2:4]}) {t[4:9]}-{t[9:]}'
        if len(t) == 12 and t.startswith('55'):
            return f'({t[2:4]}) {t[4:8]}-{t[8:]}'
        return t


class Categoria(models.Model):
    slug = models.SlugField(max_length=60, unique=True, blank=True)
    emoji = models.CharField(max_length=8, blank=True)
    nome = models.CharField(max_length=60)
    ordem = models.PositiveIntegerField(default=0)
    ativo = models.BooleanField(default=True)

    class Meta:
        ordering = ['ordem', 'nome']

    def __str__(self):
        return self.titulo

    @property
    def titulo(self) -> str:
        return f'{self.emoji} {self.nome}'.strip()

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _slug_unico(Categoria, self.nome)
        super().save(*args, **kwargs)


class Produto(models.Model):
    categoria = models.ForeignKey(Categoria, on_delete=models.PROTECT, related_name='produtos')
    slug = models.SlugField(max_length=60, unique=True, blank=True)
    nome = models.CharField(max_length=120)
    descricao = models.CharField(max_length=255, blank=True)
    preco = models.DecimalField(max_digits=8, decimal_places=2)
    imagem_url = models.URLField(blank=True)
    estoque = models.IntegerField(
        null=True, blank=True,
        help_text='Vazio = não controla estoque. Com sabores/opções, o estoque fica em cada opção.',
    )
    estoque_minimo = models.PositiveIntegerField(default=3, help_text='Alerta de estoque baixo')
    ativo = models.BooleanField(default=True)
    ordem = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['categoria__ordem', 'ordem', 'nome']

    def __str__(self):
        return self.nome

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _slug_unico(Produto, self.nome)
        super().save(*args, **kwargs)


class Variante(models.Model):
    """Sabor/cor/opção de um produto."""

    produto = models.ForeignKey(Produto, on_delete=models.CASCADE, related_name='variantes')
    slug = models.SlugField(max_length=60, unique=True, blank=True)
    nome = models.CharField(max_length=80)
    imagem_url = models.URLField(blank=True)
    estoque = models.IntegerField(null=True, blank=True, help_text='Vazio = não controla')
    ativo = models.BooleanField(default=True)
    ordem = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['ordem', 'nome']

    def __str__(self):
        return f'{self.produto.nome} - {self.nome}'

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _slug_unico(Variante, f'{self.produto.slug}-{self.nome}')
        super().save(*args, **kwargs)


def _slug_unico(modelo, texto: str) -> str:
    base = slugify(texto)[:50] or 'item'
    slug, n = base, 2
    while modelo.objects.filter(slug=slug).exists():
        slug, n = f'{base}-{n}', n + 1
    return slug


class Pedido(models.Model):
    class Status(models.TextChoices):
        AGUARDANDO_PAGAMENTO = 'aguardando_pagamento', 'Aguardando pagamento'
        COMPROVANTE_RECEBIDO = 'comprovante_recebido', 'Comprovante recebido'
        PAGO = 'pago', 'Pago'
        EM_ENTREGA = 'em_entrega', 'Saiu para entrega'
        ENTREGUE = 'entregue', 'Entregue'
        CANCELADO = 'cancelado', 'Cancelado'

    # Pedidos que contam como venda nos relatórios
    STATUS_FATURADOS = (Status.PAGO, Status.EM_ENTREGA, Status.ENTREGUE)

    cliente = models.ForeignKey(Cliente, on_delete=models.PROTECT, related_name='pedidos')
    status = models.CharField(
        max_length=30, choices=Status.choices, default=Status.AGUARDANDO_PAGAMENTO, db_index=True,
    )
    subtotal = models.DecimalField(max_digits=10, decimal_places=2)
    frete = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal('0'))
    total = models.DecimalField(max_digits=10, decimal_places=2)
    endereco = models.CharField(max_length=255, blank=True)
    endereco_mapa = models.CharField(max_length=255, blank=True)
    frete_fornecedor = models.CharField(max_length=30, blank=True)
    distancia_km = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    observacoes = models.TextField(blank=True)
    estoque_baixado = models.BooleanField(default=False)
    criado_em = models.DateTimeField(default=timezone.now, db_index=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-criado_em']

    def __str__(self):
        return f'Pedido #{self.pk}'


class ItemPedido(models.Model):
    pedido = models.ForeignKey(Pedido, on_delete=models.CASCADE, related_name='itens')
    produto = models.ForeignKey(Produto, on_delete=models.SET_NULL, null=True, blank=True)
    variante = models.ForeignKey(Variante, on_delete=models.SET_NULL, null=True, blank=True)
    nome = models.CharField(max_length=200)
    quantidade = models.PositiveIntegerField()
    preco_unitario = models.DecimalField(max_digits=8, decimal_places=2)

    def __str__(self):
        return f'{self.quantidade}x {self.nome}'

    @property
    def total(self) -> Decimal:
        return self.preco_unitario * self.quantidade


class Mensagem(models.Model):
    """Mensagem trocada no WhatsApp (para o histórico das conversas)."""

    class Autor(models.TextChoices):
        CLIENTE = 'cliente', 'Cliente'
        BOT = 'bot', 'Bot'
        ATENDENTE = 'atendente', 'Atendente'

    telefone = models.CharField(max_length=20, db_index=True)
    autor = models.CharField(max_length=10, choices=Autor.choices)
    texto = models.TextField()
    enviada = models.BooleanField(default=True, help_text='Falso se o envio pela Evolution falhou')
    criado_em = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ['criado_em']
        indexes = [models.Index(fields=['telefone', 'criado_em'])]

    def __str__(self):
        return f'{self.telefone} [{self.autor}] {self.texto[:40]}'
