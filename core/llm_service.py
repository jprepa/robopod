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


# ── Catálogo estruturado da Tabacaria ──
CATALOGO: dict[str, Any] = {
    'categorias': [
        {
            'id': 'narguiles',
            'titulo': '🔥 Narguilés',
            'itens': [
                {
                    'id': 'sessao_robopod',
                    'nome': 'Sessão Robopod (completa)',
                    'preco': 89.90,
                    'descricao': 'Narguilé preparado e completo para sua sessão',
                    'imagem_url': '',
                    'variantes': [],
                },
            ],
        },
        {
            'id': 'essencias',
            'titulo': '🍬 Essências',
            'itens': [
                {
                    'id': 'essencia_premium',
                    'nome': 'Essência Premium (50g)',
                    'preco': 25.00,
                    'descricao': 'Essência premium de alta qualidade',
                    'imagem_url': '',
                    'variantes': [
                        {'id': 'ep_menta', 'nome': 'Menta', 'imagem_url': ''},
                        {'id': 'ep_morango', 'nome': 'Morango', 'imagem_url': ''},
                        {'id': 'ep_uva', 'nome': 'Uva', 'imagem_url': ''},
                        {'id': 'ep_tutti_frutti', 'nome': 'Tutti Frutti', 'imagem_url': ''},
                    ],
                },
                {
                    'id': 'essencia_gold',
                    'nome': 'Essência Gold (50g)',
                    'preco': 35.00,
                    'descricao': 'Essência gold linha especial',
                    'imagem_url': '',
                    'variantes': [
                        {'id': 'eg_love66', 'nome': 'Love 66', 'imagem_url': ''},
                        {'id': 'eg_double_apple', 'nome': 'Double Apple', 'imagem_url': ''},
                        {'id': 'eg_blueberry', 'nome': 'Blueberry Mint', 'imagem_url': ''},
                    ],
                },
            ],
        },
        {
            'id': 'carvao',
            'titulo': '🪨 Carvão',
            'itens': [
                {
                    'id': 'carvao_hexagonal',
                    'nome': 'Carvão Hexagonal (caixa 250g)',
                    'preco': 15.00,
                    'descricao': 'Carvão hexagonal de coco 250g',
                    'imagem_url': '',
                    'variantes': [],
                },
                {
                    'id': 'carvao_coco',
                    'nome': 'Carvão Coco (caixa 500g)',
                    'preco': 28.00,
                    'descricao': 'Carvão de coco premium 500g',
                    'imagem_url': '',
                    'variantes': [],
                },
            ],
        },
        {
            'id': 'acessorios',
            'titulo': '🧴 Acessórios',
            'itens': [
                {
                    'id': 'piteira',
                    'nome': 'Piteira descartável (10 un.)',
                    'preco': 8.00,
                    'descricao': 'Pacote com 10 piteiras descartáveis',
                    'imagem_url': '',
                    'variantes': [],
                },
                {
                    'id': 'mangueira',
                    'nome': 'Mangueira lavável',
                    'preco': 22.00,
                    'descricao': 'Mangueira de silicone lavável',
                    'imagem_url': '',
                    'variantes': [
                        {'id': 'mg_preta', 'nome': 'Preta', 'imagem_url': ''},
                        {'id': 'mg_transparente', 'nome': 'Transparente', 'imagem_url': ''},
                        {'id': 'mg_azul', 'nome': 'Azul', 'imagem_url': ''},
                    ],
                },
                {
                    'id': 'base_vaso',
                    'nome': 'Base / vaso de reposição',
                    'preco': 45.00,
                    'descricao': 'Vaso de vidro para reposição',
                    'imagem_url': '',
                    'variantes': [],
                },
                {
                    'id': 'rosh',
                    'nome': 'Rosh / Cabeça de cerâmica',
                    'preco': 30.00,
                    'descricao': 'Rosh de cerâmica artesanal',
                    'imagem_url': '',
                    'variantes': [],
                },
            ],
        },
        {
            'id': 'bebidas',
            'titulo': '🥤 Bebidas',
            'itens': [
                {
                    'id': 'agua',
                    'nome': 'Água (500ml)',
                    'preco': 5.00,
                    'descricao': 'Água mineral 500ml',
                    'imagem_url': '',
                    'variantes': [],
                },
                {
                    'id': 'refrigerante',
                    'nome': 'Refrigerante lata (350ml)',
                    'preco': 7.00,
                    'descricao': 'Refrigerante lata 350ml',
                    'imagem_url': '',
                    'variantes': [
                        {'id': 'ref_coca', 'nome': 'Coca-Cola', 'imagem_url': ''},
                        {'id': 'ref_guarana', 'nome': 'Guaraná', 'imagem_url': ''},
                        {'id': 'ref_sprite', 'nome': 'Sprite', 'imagem_url': ''},
                    ],
                },
                {
                    'id': 'suco',
                    'nome': 'Suco natural (300ml)',
                    'preco': 10.00,
                    'descricao': 'Suco natural da fruta 300ml',
                    'imagem_url': '',
                    'variantes': [
                        {'id': 'sc_laranja', 'nome': 'Laranja', 'imagem_url': ''},
                        {'id': 'sc_maracuja', 'nome': 'Maracujá', 'imagem_url': ''},
                    ],
                },
                {
                    'id': 'energetico',
                    'nome': 'Energético (250ml)',
                    'preco': 12.00,
                    'descricao': 'Energético lata 250ml',
                    'imagem_url': '',
                    'variantes': [],
                },
            ],
        },
    ],
}


# ── Funções auxiliares do catálogo ──

def formatar_preco(valor: float) -> str:
    """Formata valor no padrão brasileiro (ex.: 1234.5 -> 'R$ 1.234,50')."""
    return f"R$ {valor:,.2f}".replace(',', '_').replace('.', ',').replace('_', '.')


def gerar_menu_texto() -> str:
    """Gera o cardápio formatado para WhatsApp (negrito/itálico) e para o prompt.

    Os itens são numerados em sequência para que o cliente possa pedir
    pelo número ("quero o 3") — o mesmo texto vai no system prompt, então
    a IA sabe a qual item cada número corresponde.
    """
    linhas: list[str] = [
        '📋 *CARDÁPIO ROBOPOD*',
        '_Delivery noturno • 18h às 03h_',
        '',
    ]

    numero = 1
    for categoria in CATALOGO['categorias']:
        emoji, _, nome_cat = categoria['titulo'].partition(' ')
        linhas.append(f'{emoji} *{nome_cat.upper()}*')

        for item in categoria['itens']:
            linhas.append(f"{numero}. {item['nome']} — *{formatar_preco(item['preco'])}*")
            if item['variantes']:
                nomes_var = ', '.join(v['nome'] for v in item['variantes'])
                linhas.append(f'    ↳ _{nomes_var}_')
            numero += 1

        linhas.append('')  # linha em branco entre categorias

    linhas.append('━━━━━━━━━━━━━━━')
    linhas.append(
        '👉 Para pedir, é só escrever o que deseja ou o número do item.\n'
        '_Ex.: "2 essência premium menta e 1 carvão coco"_'
    )
    return '\n'.join(linhas)


# Limites da mensagem de lista do WhatsApp — acima disso a lista é rejeitada
# ou simplesmente não aparece para o cliente.
_LISTA_MAX_LINHAS = 10
_LISTA_MAX_TITULO = 24
_LISTA_MAX_DESCRICAO = 72


def _truncar(texto: str, limite: int) -> str:
    return texto if len(texto) <= limite else texto[:limite - 1] + '…'


def gerar_secoes_lista() -> list[dict]:
    """Gera a lista interativa de CATEGORIAS (primeiro nível do cardápio).

    O WhatsApp aceita no máximo 10 linhas por lista, então o cardápio é
    navegado em dois níveis: categoria -> itens (ver `gerar_secoes_categoria`).
    Cada row tem rowId = 'cat:<id_categoria>'.

    Returns:
        Lista de seções no formato da Evolution API.
    """
    rows: list[dict] = []
    for categoria in CATALOGO['categorias'][:_LISTA_MAX_LINHAS]:
        precos = [i['preco'] for i in categoria['itens']]
        rows.append({
            'rowId': f"cat:{categoria['id']}",
            'title': _truncar(categoria['titulo'], _LISTA_MAX_TITULO),
            'description': _truncar(
                f"{len(precos)} {'opção' if len(precos) == 1 else 'opções'} • "
                f"a partir de {formatar_preco(min(precos))}",
                _LISTA_MAX_DESCRICAO,
            ),
        })
    return [{'title': 'Categorias', 'rows': rows}]


def buscar_categoria(categoria_id: str) -> dict | None:
    """Busca uma categoria no catálogo pelo ID."""
    for categoria in CATALOGO['categorias']:
        if categoria['id'] == categoria_id:
            return categoria
    return None


def gerar_secoes_categoria(categoria_id: str) -> list[dict] | None:
    """Gera a lista interativa com os itens de uma categoria (segundo nível).

    Returns:
        Lista de seções no formato da Evolution API, ou None se a categoria
        não existir.
    """
    categoria = buscar_categoria(categoria_id)
    if categoria is None:
        return None

    rows: list[dict] = []
    for item in categoria['itens'][:_LISTA_MAX_LINHAS]:
        rows.append({
            'rowId': item['id'],
            'title': _truncar(item['nome'], _LISTA_MAX_TITULO),
            'description': _truncar(
                f"{formatar_preco(item['preco'])} • {item['descricao']}",
                _LISTA_MAX_DESCRICAO,
            ),
        })
    return [{'title': _truncar(categoria['titulo'], _LISTA_MAX_TITULO), 'rows': rows}]


def gerar_secoes_variantes(item_id: str) -> list[dict] | None:
    """Gera seções de variantes para um item específico (sendList).

    Localiza o item pelo ID em todas as categorias. Se possuir variantes,
    retorna uma lista de seções pronta para o formato Evolution API.

    Returns:
        Lista de seções com as variantes, ou None se o item não tem variantes.
    """
    item = buscar_item(item_id)
    if item is None or not item['variantes']:
        return None

    rows: list[dict] = []
    for variante in item['variantes'][:_LISTA_MAX_LINHAS]:
        rows.append({
            'rowId': variante['id'],
            'title': _truncar(variante['nome'], _LISTA_MAX_TITULO),
        })

    return [{'title': 'Opções', 'rows': rows}]


def buscar_item(item_id: str) -> dict | None:
    """Busca um item no catálogo pelo ID."""
    for categoria in CATALOGO['categorias']:
        for item in categoria['itens']:
            if item['id'] == item_id:
                return item
    return None


def buscar_variante(item_id: str, variante_id: str) -> dict | None:
    """Busca uma variante específica de um item."""
    item = buscar_item(item_id)
    if item is None:
        return None
    for variante in item['variantes']:
        if variante['id'] == variante_id:
            return variante
    return None


def item_tem_variantes(item_id: str) -> bool:
    """Verifica se um item possui variantes (sabores/cores)."""
    item = buscar_item(item_id)
    if item is None:
        return False
    return len(item['variantes']) > 0


# ── Menu da Tabacaria (gerado dinamicamente a partir do catálogo) ──
MENU_TABACARIA = gerar_menu_texto()

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
11. Se o cliente pedir o cardápio, o menu, os preços ou perguntar o que tem disponível, use a ferramenta `mostrar_cardapio` em vez de escrever o cardápio você mesmo.
12. Os itens do cardápio são numerados. Se o cliente pedir pelo número (ex.: "quero o 3"), use o item correspondente.
13. Se o item escolhido tiver sabores/opções e o cliente não disser qual, pergunte antes de seguir. No `fechar_pedido`, inclua a opção no nome (ex.: "Essência Premium (50g) - Menta").

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
    {
        "type": "function",
        "function": {
            "name": "mostrar_cardapio",
            "description": (
                "Envia o cardápio completo e formatado ao cliente. Chamada "
                "quando o cliente pede o cardápio, o menu, os preços ou "
                "pergunta o que está disponível."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


# ── Tipos de retorno ──
class LLMResult:
    """Resultado do processamento da LLM."""

    def __init__(
        self,
        tipo: str,  # 'texto' | 'fechar_pedido' | 'solicitar_humano' | 'mostrar_cardapio'
        conteudo: str = '',
        dados: dict[str, Any] | None = None,
    ):
        self.tipo = tipo
        self.conteudo = conteudo
        self.dados = dados or {}

    def __repr__(self):
        return f'LLMResult(tipo={self.tipo!r}, conteudo={self.conteudo[:50]!r})'


def _reparar_historico(historico: list[dict]) -> list[dict]:
    """Garante que toda mensagem com tool_calls tenha as respostas 'tool'.

    Sessões gravadas antes desta correção guardavam o tool_call sem resposta,
    o que faz a OpenAI rejeitar (erro 400) todas as mensagens seguintes.
    """
    reparado: list[dict] = []
    pendentes: list[str] = []
    for msg in historico:
        if msg.get('role') == 'tool':
            if msg.get('tool_call_id') in pendentes:
                pendentes.remove(msg['tool_call_id'])
        else:
            reparado.extend(
                {'role': 'tool', 'tool_call_id': tc_id, 'content': 'ok'}
                for tc_id in pendentes
            )
            pendentes = []
        reparado.append(msg)
        if msg.get('role') == 'assistant' and msg.get('tool_calls'):
            pendentes = [tc['id'] for tc in msg['tool_calls']]
    reparado.extend(
        {'role': 'tool', 'tool_call_id': tc_id, 'content': 'ok'} for tc_id in pendentes
    )
    return reparado


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
    historico = _reparar_historico(historico)

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
        try:
            argumentos = json.loads(tool_call.function.arguments or '{}')
        except json.JSONDecodeError:
            argumentos = {}

        logger.info('Function call: %s -> %s', nome_funcao, argumentos)

        conteudos = {
            'fechar_pedido': 'Pedido fechado com sucesso!',
            'solicitar_humano': 'Transferindo para atendente humano.',
            'mostrar_cardapio': 'Cardápio enviado ao cliente.',
        }

        # Adiciona a mensagem do assistente com tool_calls ao histórico,
        # seguida de uma resposta 'tool' para CADA chamada — a OpenAI rejeita
        # (erro 400) qualquer histórico com tool_calls sem resposta.
        novo_historico.append({
            "role": "assistant",
            "content": message.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in message.tool_calls
            ],
        })
        for tc in message.tool_calls:
            novo_historico.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": conteudos.get(tc.function.name, 'ok'),
            })

        if nome_funcao in conteudos:
            return (
                LLMResult(
                    tipo=nome_funcao,
                    conteudo=conteudos[nome_funcao],
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
