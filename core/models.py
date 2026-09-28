from django.db import models
from django.utils import timezone


class SessaoChat(models.Model):
    """Sessão de conversa com um cliente via WhatsApp.

    Cada número de telefone possui uma única sessão ativa.
    O campo `estado_atual` controla a máquina de estados do fluxo híbrido.
    """

    class Estado(models.TextChoices):
        ATENDIMENTO_LLM = 'atendimento_llm', 'Atendimento LLM'
        AGUARDANDO_ENDERECO = 'aguardando_endereco', 'Aguardando Endereço'
        CALCULANDO_FRETE = 'calculando_frete', 'Calculando Frete'
        AGUARDANDO_COMPROVANTE = 'aguardando_comprovante', 'Aguardando Comprovante'
        ATENDIMENTO_HUMANO = 'atendimento_humano', 'Atendimento Humano'

    telefone = models.CharField(
        max_length=20,
        unique=True,
        db_index=True,
        help_text='Número do WhatsApp no formato 55XXXXXXXXXXX',
    )
    estado_atual = models.CharField(
        max_length=30,
        choices=Estado.choices,
        default=Estado.ATENDIMENTO_LLM,
    )
    carrinho_temporario = models.JSONField(
        default=dict,
        blank=True,
        help_text='Carrinho gerado pela function calling da LLM: {"itens": [...]}',
    )
    historico_mensagens = models.JSONField(
        default=list,
        blank=True,
        help_text='Histórico completo de mensagens para contexto da OpenAI',
    )
    ultima_interacao = models.DateTimeField(
        default=timezone.now,
        db_index=True,
    )
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Sessão de Chat'
        verbose_name_plural = 'Sessões de Chat'
        ordering = ['-ultima_interacao']

    def __str__(self):
        return f'{self.telefone} [{self.get_estado_atual_display()}]'

    def resetar_sessao(self):
        """Reseta a sessão para o estado inicial (novo atendimento).

        Não salva: o chamador deve usar `save()`/`asave()` — um `save()`
        síncrono aqui quebra (SynchronousOnlyOperation) dentro da view async.
        """
        self.estado_atual = self.Estado.ATENDIMENTO_LLM
        self.carrinho_temporario = {}
        self.historico_mensagens = []
        self.ultima_interacao = timezone.now()
