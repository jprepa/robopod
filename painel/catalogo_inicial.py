"""Cardápio inicial gravado no banco pela migração 0002 (antes ficava fixo em
core/llm_service.py). Depois disso o cardápio é editado pelo painel."""

CATALOGO_INICIAL = {
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
