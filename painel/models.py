"""Dados da loja exibidos no painel: clientes, catálogo/estoque, pedidos e
o histórico de mensagens do WhatsApp."""

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.urls import NoReverseMatch, reverse
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

    class Origem(models.TextChoices):
        WHATSAPP = 'whatsapp', 'WhatsApp'
        MANUAL = 'manual', 'Venda manual'

    # Pedidos que contam como venda nos relatórios
    STATUS_FATURADOS = (Status.PAGO, Status.EM_ENTREGA, Status.ENTREGUE)

    cliente = models.ForeignKey(
        Cliente, on_delete=models.PROTECT, related_name='pedidos', null=True, blank=True,
        help_text='Vazio em vendas manuais sem cliente identificado (balcão)',
    )
    origem = models.CharField(max_length=10, choices=Origem.choices, default=Origem.WHATSAPP)
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
    vendedor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text='Quem lançou a venda manual ou aprovou o pagamento do pedido do WhatsApp',
    )
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


HORARIO_PADRAO = 'Todos os dias, das 18h às 03h'
MENSAGEM_FECHADO_PADRAO = (
    '🌙 Oi! No momento a *Robopod* está fechada e o atendimento está pausado.\n'
    'Assim que abrirmos, é só mandar mensagem que a gente te atende 😉'
)


class Configuracao(models.Model):
    """Configurações da loja editadas no painel (registro único, pk=1)."""

    grupo_avisos_id = models.CharField(
        max_length=80, blank=True, help_text='ID do grupo do WhatsApp (termina em @g.us)',
    )
    grupo_avisos_nome = models.CharField(max_length=120, blank=True)
    avisos_ativos = models.BooleanField(default=True)
    avisar_mensagens_atendimento = models.BooleanField(
        default=True,
        help_text='Repassa ao grupo as mensagens de clientes em atendimento humano',
    )
    cardapio_pdf = models.FileField(
        upload_to='cardapio/', blank=True,
        help_text='PDF do cardápio enviado ao cliente junto das boas-vindas',
    )
    expediente_aberto = models.BooleanField(
        default=True, help_text='Fechado: o bot só responde com a mensagem de loja fechada',
    )
    expediente_fechado_em = models.DateTimeField(null=True, blank=True)
    bot_pausado = models.BooleanField(
        default=False,
        help_text='Pausado: o bot não responde ninguém (nem com a mensagem de loja fechada)',
    )
    bot_pausado_em = models.DateTimeField(null=True, blank=True)
    expediente_aberto_em = models.DateTimeField(null=True, blank=True)
    horario_atendimento = models.CharField(max_length=120, blank=True, default=HORARIO_PADRAO)
    mensagem_fechado = models.TextField(blank=True, default=MENSAGEM_FECHADO_PADRAO)

    class Meta:
        verbose_name = 'Configuração'
        verbose_name_plural = 'Configurações'

    def __str__(self):
        return 'Configurações da loja'

    @property
    def texto_fechado(self) -> str:
        """Mensagem enviada ao cliente que chama com a loja fechada."""
        partes = [self.mensagem_fechado.strip() or MENSAGEM_FECHADO_PADRAO]
        if self.horario_atendimento.strip():
            partes.append(f'🕒 *Horário de atendimento:* {self.horario_atendimento.strip()}')
        return '\n\n'.join(partes)

    @classmethod
    def obter(cls) -> 'Configuracao':
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @classmethod
    async def aobter(cls) -> 'Configuracao':
        obj, _ = await cls.objects.aget_or_create(pk=1)
        return obj


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


class RegistroAuditoria(models.Model):
    """Quem fez o quê no painel (e o que o bot fez sozinho). Só é gravado,
    nunca editado: não há tela para alterar nem apagar registros."""

    class Tipo(models.TextChoices):
        ACESSO = 'acesso', 'Acesso'
        CONVERSA = 'conversa', 'Conversa'
        CLIENTE = 'cliente', 'Cliente'
        PEDIDO = 'pedido', 'Pedido'
        ESTOQUE = 'estoque', 'Estoque'
        CONFIGURACAO = 'configuracao', 'Configurações'

    BOT = 'Bot'

    criado_em = models.DateTimeField(default=timezone.now, db_index=True)
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    # Guardado em texto para continuar legível se o usuário for excluído
    responsavel = models.CharField(max_length=150, db_index=True, help_text='Usuário, ou "Bot"')
    tipo = models.CharField(max_length=20, choices=Tipo.choices, db_index=True)
    acao = models.CharField(max_length=40, help_text='Identificador da ação, ex.: pedido_status')
    descricao = models.CharField(max_length=255)
    mudancas = models.JSONField(default=dict, blank=True, help_text='{campo: [antes, depois]}')
    alvo_tipo = models.CharField(max_length=20, blank=True)
    alvo_id = models.CharField(max_length=40, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ['-criado_em', '-pk']
        verbose_name = 'Registro de auditoria'
        verbose_name_plural = 'Auditoria'
        indexes = [models.Index(fields=['alvo_tipo', 'alvo_id'], name='painel_auditoria_alvo_idx')]

    def __str__(self):
        return f'{self.criado_em:%d/%m/%Y %H:%M} {self.responsavel}: {self.descricao}'

    _LINKS = {
        'pedido': lambda i: reverse('painel:pedido', args=[int(i)]),
        'cliente': lambda i: reverse('painel:cliente', args=[int(i)]),
        'produto': lambda i: reverse('painel:produto_editar', args=[int(i)]),
        'conversa': lambda i: reverse('painel:conversa', args=[i]),
        'categoria': lambda i: reverse('painel:categorias') + f'?editar={int(i)}',
        'configuracao': lambda i: reverse('painel:configuracoes'),
    }

    @property
    def link(self) -> str:
        gerar = self._LINKS.get(self.alvo_tipo)
        if not gerar or not self.alvo_id:
            return ''
        try:
            return gerar(self.alvo_id)
        except (NoReverseMatch, ValueError):
            return ''


class RespostaRapida(models.Model):
    """Mensagem pronta do atendente no chat: vira botão acima da caixa de
    texto e/ou comando "/atalho". Aceita variáveis como {nome} e {pix}."""

    titulo = models.CharField(max_length=40, help_text='Texto do botão, ex.: Agradecimento')
    atalho = models.SlugField(
        max_length=30, unique=True,
        help_text='Comando digitado no chat, sem a barra. Ex.: obrigado → /obrigado',
    )
    texto = models.TextField()
    mostrar_botao = models.BooleanField(
        default=True, help_text='Desmarcado: só aparece digitando o comando',
    )
    ordem = models.PositiveIntegerField(default=0)
    ativo = models.BooleanField(default=True)

    class Meta:
        ordering = ['ordem', 'titulo']
        verbose_name = 'Resposta rápida'
        verbose_name_plural = 'Respostas rápidas'

    def __str__(self):
        return f'/{self.atalho} ({self.titulo})'
