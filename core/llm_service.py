"""Serviço de integração com a API da OpenAI.

Implementa o fluxo conversacional com guardrails estritos e
Function Calling para fechar pedidos ou escalar para humano.
"""

import json
import logging
from typing import Any

from django.conf import settings
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

# ── Cliente OpenAI (singleton por módulo) ──
_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    return _client


# ── Menu da Tabacaria (RAG Base Estática) ──
MENU_TABACARIA = """
📋 CARDÁPIO ROBOPOD — DELIVERY NOTURNO

🔥 NARGUILÉS
  • Sessão Robopod (completa)       — R$ 89,90

🍬 ESSÊNCIAS (avulsas)
  • Essência Premium (50g)          — R$ 25,00
  • Essência Gold (50g)             — R$ 35,00

🪨 CARVÃO
  • Carvão Hexagonal (caixa 250g)   — R$ 15,00
  • Carvão Coco (caixa 500g)        — R$ 28,00

🧴 ACESSÓRIOS
  • Piteira descartável (10 un.)    — R$ 8,00
  • Mangueira lavável               — R$ 22,00
  • Base / vaso de reposição        — R$ 45,00
  • Rosh / Cabeça de cerâmica       — R$ 30,00

🥤 BEBIDAS
  • Água (500ml)                    — R$ 5,00
  • Refrigerante lata (350ml)       — R$ 7,00
  • Suco natural (300ml)            — R$ 10,00
  • Energético (250ml)              — R$ 12,00
"""

SYSTEM_PROMPT = f"""Você é o atendente virtual da Robopod, uma tabacaria delivery noturna.

REGRAS INVIOLÁVEIS:
1. Você SÓ pode oferecer os itens listados no CARDÁPIO abaixo. Se o cliente pedir algo que não existe no cardápio, diga educadamente que não está disponível.
2. NUNCA invente produtos, preços ou promoções.
3. NUNCA ofereça descontos ou altere preços.
4. Respostas devem ser CURTAS e objetivas (máximo 3 frases por mensagem).
5. Seja simpático e use emojis com moderação.
6. Se o cliente perguntar sobre algo fora do escopo da tabacaria (política, saúde, etc.), redirecione educadamente para o cardápio.
7. Se o cliente pedir para falar com um humano, ou se você não souber responder, use IMEDIATAMENTE a ferramenta `solicitar_humano`.
8. Quando o cliente confirmar que quer finalizar o pedido, use a ferramenta `fechar_pedido` com os itens e quantidades corretos.
9. Sempre confirme o pedido com o cliente ANTES de chamar `fechar_pedido`.
10. Horário de funcionamento: 18h às 03h. Fora desse horário, informe que estamos fechados.

CARDÁPIO:
{MENU_TABACARIA}
"""

# ── Definição das Tools (Function Calling) ──
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "fechar_pedido",
            "description": (
                "Chamada quando o cliente CONFIRMA a compra. "
                "Gera o carrinho com os itens e quantidades do pedido."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "itens": {
                        "type": "array",
                        "description": "Lista de itens do pedido",
                        "items": {
                            "type": "object",
                            "properties": {
                                "nome": {
                                    "type": "string",
                                    "description": "Nome do item exatamente como no cardápio",
                                },
                                "quantidade": {
                                    "type": "integer",
                                    "description": "Quantidade do item",
                                    "minimum": 1,
                                },
                                "preco_unitario": {
                                    "type": "number",
                                    "description": "Preço unitário do item em reais",
                                },
                            },
                            "required": ["nome", "quantidade", "preco_unitario"],
                        },
                    },
                },
                "required": ["itens"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "solicitar_humano",
            "description": (
                "Botão de pânico. Chamada quando: "
                "(a) o cliente pede para falar com um atendente humano, "
                "(b) a IA não sabe responder, "
                "(c) o assunto foge do escopo da tabacaria."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "motivo": {
                        "type": "string",
                        "description": "Breve motivo da transferência para humano",
                    },
                },
                "required": ["motivo"],
            },
        },
    },
]


# ── Tipos de retorno ──
class LLMResult:
    """Resultado do processamento da LLM."""

    def __init__(
        self,
        tipo: str,  # 'texto' | 'fechar_pedido' | 'solicitar_humano'
        conteudo: str = '',
        dados: dict[str, Any] | None = None,
    ):
        self.tipo = tipo
        self.conteudo = conteudo
        self.dados = dados or {}

    def __repr__(self):
        return f'LLMResult(tipo={self.tipo!r}, conteudo={self.conteudo[:50]!r})'


async def processar_mensagem(
    historico: list[dict[str, str]],
    mensagem_usuario: str,
) -> tuple[LLMResult, list[dict[str, str]]]:
    """Processa uma mensagem do usuário via OpenAI Chat Completions.

    Args:
        historico: Lista de mensagens anteriores (formato OpenAI).
        mensagem_usuario: Texto enviado pelo cliente.

    Returns:
        Tupla (resultado, historico_atualizado).
    """
    client = _get_client()

    # Monta mensagens com system prompt + histórico + nova mensagem
    mensagens = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *historico,
        {"role": "user", "content": mensagem_usuario},
    ]

    try:
        response = await client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=mensagens,
            tools=TOOLS,
            tool_choice="auto",
            temperature=0.3,
            max_tokens=500,
        )
    except Exception:
        logger.exception('Erro ao chamar OpenAI API')
        return (
            LLMResult(
                tipo='texto',
                conteudo='😕 Desculpe, estou com um problema técnico. Tente novamente em instantes.',
            ),
            historico,
        )

    choice = response.choices[0]
    message = choice.message

    # Atualiza histórico
    novo_historico = [
        *historico,
        {"role": "user", "content": mensagem_usuario},
    ]

    # ── Verifica se houve Function Calling ──
    if message.tool_calls:
        tool_call = message.tool_calls[0]
        nome_funcao = tool_call.function.name
        argumentos = json.loads(tool_call.function.arguments)

        logger.info('Function call: %s -> %s', nome_funcao, argumentos)

        # Adiciona a mensagem do assistente com tool_calls ao histórico
        novo_historico.append(message.model_dump(exclude_none=True))

        if nome_funcao == 'fechar_pedido':
            return (
                LLMResult(
                    tipo='fechar_pedido',
                    conteudo='Pedido fechado com sucesso!',
                    dados=argumentos,
                ),
                novo_historico,
            )

        if nome_funcao == 'solicitar_humano':
            return (
                LLMResult(
                    tipo='solicitar_humano',
                    conteudo='Transferindo para atendente humano.',
                    dados=argumentos,
                ),
                novo_historico,
            )

    # ── Resposta de texto simples ──
    texto_resposta = message.content or ''
    novo_historico.append({"role": "assistant", "content": texto_resposta})

    return (
        LLMResult(tipo='texto', conteudo=texto_resposta),
        novo_historico,
    )
