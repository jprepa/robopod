"""Dados da loja exibidos no painel: clientes, catálogo/estoque, pedidos e
o histórico de mensagens do WhatsApp."""

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.text import slugify


class Loja(models.Model):
    """Número de WhatsApp da operação (uma instância da Evolution). As lojas
    dividem cardápio, estoque, equipe e configurações; cada uma pode ter os
    próprios preços (`PrecoLoja`) e o próprio PDF do cardápio."""

    nome = models.CharField(max_length=60)
    instancia = models.CharField(
        max_length=60, unique=True,
        help_text='Nome da instância na Evolution (letras, números, - e _)',
    )
    principal = models.BooleanField(
        default=False,
        help_text='Número que manda os avisos ao grupo da equipe (precisa participar do grupo)',
    )
    ativa = models.BooleanField(
        default=True, help_text='Desativada: o bot ignora as mensagens que chegam por este número',
    )
    cardapio_pdf = models.FileField(
        upload_to='cardapio/', blank=True,
        help_text='PDF do cardápio desta loja. Vazio = o PDF de Configurações',
    )
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-principal', 'pk']

    def __str__(self):
        return self.nome


class Cliente(models.Model):
    """Cliente identificado pelo número do WhatsApp, o mesmo em todas as lojas."""

    telefone = models.CharField(max_length=20, unique=True, help_text='Formato 55XXXXXXXXXXX')
    nome = models.CharField(max_length=120, blank=True)
    nome_whatsapp = models.CharField(max_length=120, blank=True, help_text='Nome do perfil no WhatsApp')
    observacoes = models.TextField(blank=True)
    bloqueado = models.BooleanField(default=False, help_text='O bot ignora mensagens deste número')
    loja_origem = models.ForeignKey(
        Loja, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text='Número que o cliente chamou na primeira vez',
    )
    loja_atual = models.ForeignKey(
        Loja, on_delete=models.SET_NULL, null=True, blank=True, related_name='clientes',
        help_text='Número da última mensagem do cliente: bot e atendentes respondem por ele',
    )
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
    preco_revenda = models.DecimalField(
        'Preço de revenda', max_digits=8, decimal_places=2, null=True, blank=True,
        help_text='Usado nas vendas para revendedor. Vazio = mesmo preço do cardápio.',
    )
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


class PrecoLoja(models.Model):
    """Preço de um produto numa loja. Sem registro, vale `Produto.preco`."""

    loja = models.ForeignKey(Loja, on_delete=models.CASCADE, related_name='precos')
    produto = models.ForeignKey(Produto, on_delete=models.CASCADE, related_name='precos_loja')
    preco = models.DecimalField(max_digits=8, decimal_places=2)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['loja', 'produto'], name='preco_unico_por_loja')]

    def __str__(self):
        return f'{self.produto.nome} na {self.loja.nome}: {self.preco}'


def precos_da_loja(loja_id: int | None) -> dict[int, Decimal]:
    """{produto_id: preço} dos produtos com preço próprio nesta loja."""
    if not loja_id:
        return {}
    return dict(PrecoLoja.objects.filter(loja_id=loja_id).values_list('produto_id', 'preco'))


def _slug_unico(modelo, texto: str) -> str:
    base = slugify(texto)[:50] or 'item'
    slug, n = base, 2
    while modelo.objects.filter(slug=slug).exists():
        slug, n = f'{base}-{n}', n + 1
    return slug


class Revendedor(models.Model):
    """Quem compra da loja para revender. As vendas para ele saem com o preço de
    revenda e ficam a receber até o acerto (`Pedido.acertado_em`)."""

    nome = models.CharField(max_length=120)
    telefone = models.CharField(max_length=20, blank=True, help_text='Formato 55XXXXXXXXXXX')
    observacoes = models.TextField(blank=True)
    ativo = models.BooleanField(default=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['nome']
        verbose_name_plural = 'Revendedores'

    def __str__(self):
        return self.nome


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
        GRUPO = 'grupo', 'Lançada no grupo'

    # Pedidos que contam como venda nos relatórios
    STATUS_FATURADOS = (Status.PAGO, Status.EM_ENTREGA, Status.ENTREGUE)

    cliente = models.ForeignKey(
        Cliente, on_delete=models.PROTECT, related_name='pedidos', null=True, blank=True,
        help_text='Vazio em vendas manuais sem cliente identificado (balcão)',
    )
    origem = models.CharField(max_length=10, choices=Origem.choices, default=Origem.WHATSAPP)
    loja = models.ForeignKey(
        Loja, on_delete=models.SET_NULL, null=True, blank=True, related_name='pedidos',
        help_text='Número por onde o pedido foi feito, ou a loja escolhida na venda manual',
    )
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
    comprovante = models.FileField(
        upload_to='comprovantes/', blank=True,
        help_text='Imagem ou PDF do comprovante PIX enviado pelo cliente no WhatsApp',
    )
    estoque_baixado = models.BooleanField(default=False)
    revendedor = models.ForeignKey(
        Revendedor, on_delete=models.PROTECT, null=True, blank=True, related_name='pedidos',
        help_text='Venda para revendedor: preço de revenda, a receber até o acerto',
    )
    acertado_em = models.DateTimeField(
        null=True, blank=True, help_text='Quando o revendedor pagou esta venda (vazio = a receber)',
    )
    criado_em = models.DateTimeField(default=timezone.now, db_index=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-criado_em']

    def __str__(self):
        return f'Pedido #{self.pk}'

    @property
    def a_receber(self) -> bool:
        return bool(self.revendedor_id) and not self.acertado_em and self.status != self.Status.CANCELADO

    @property
    def comprovante_eh_pdf(self) -> bool:
        return self.comprovante.name.lower().endswith('.pdf')


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
        help_text='PDF do cardápio enviado ao cliente junto das boas-vindas (lojas sem PDF próprio)',
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
    vendas_pelo_grupo = models.BooleanField(
        default=True,
        help_text='A equipe lança vendas mandando "venda ...", "saiu ..." ou "lançar ..." no grupo de avisos',
    )

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
    loja = models.ForeignKey(
        Loja, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text='Número por onde a mensagem entrou ou saiu',
    )
    autor = models.CharField(max_length=10, choices=Autor.choices)
    texto = models.TextField()
    midia = models.FileField(
        upload_to='conversas/', blank=True,
        help_text='Foto ou PDF enviado pelo cliente, para o atendente ver no painel',
    )
    enviada = models.BooleanField(default=True, help_text='Falso se o envio pela Evolution falhou')
    criado_em = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ['criado_em']
        indexes = [models.Index(fields=['telefone', 'criado_em'])]

    def __str__(self):
        return f'{self.telefone} [{self.autor}] {self.texto[:40]}'

    @property
    def midia_eh_pdf(self) -> bool:
        return self.midia.name.lower().endswith('.pdf')

    @property
    def legenda(self) -> str:
        """Texto sem o rótulo "[imagem]"/"[documento]", que sobra quando a
        mídia em si aparece no balão."""
        for rotulo in ('[imagem]', '[documento]'):
            if self.texto.startswith(rotulo):
                return self.texto.removeprefix(rotulo).strip()
        return self.texto


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


class InscricaoPush(models.Model):
    """Celular ou navegador que ativou as notificações do painel (Web Push).

    Os dados vêm do navegador (`pushManager.subscribe`). O serviço `notificacoes`
    do docker-compose manda os avisos para cá; o endpoint some sozinho quando o
    aparelho desinstala o app ou revoga a permissão.
    """

    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='+')
    endpoint = models.URLField(max_length=1000, unique=True)
    p256dh = models.CharField(max_length=200)
    auth = models.CharField(max_length=100)
    eventos = models.JSONField(default=list, blank=True, help_text='Eventos que este aparelho quer receber')
    aparelho = models.CharField(max_length=60, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    ultimo_envio = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Inscrição em notificações'
        verbose_name_plural = 'Inscrições em notificações'

    def __str__(self):
        return f'{self.usuario} · {self.aparelho or "aparelho"}'


class PerfilEquipe(models.Model):
    """Número do WhatsApp de quem usa o painel: a venda lançada no grupo por
    esse número fica com essa pessoa como vendedora."""

    usuario = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='perfil_equipe',
    )
    whatsapp = models.CharField(max_length=20, unique=True, help_text='Formato 55XXXXXXXXXXX')

    def __str__(self):
        return f'{self.usuario} ({self.whatsapp})'
