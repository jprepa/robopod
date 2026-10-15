"""Testes das mensagens de voz no webhook da Evolution API.

A Evolution e a OpenAI são simuladas: nada sai para a rede.

    python manage.py test core
"""

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from django.test import TestCase, override_settings

from painel.models import Cliente, Configuracao, Mensagem

from .llm_service import LLMResult
from .models import SessaoChat
from .views import _extrair_dados_webhook

TELEFONE = '5511999998888'
AUDIO_FALSO = b'OggS-audio-de-teste'
GRUPO = '120363000000000000@g.us'


def _payload_audio(segundos: int = 8, base64_inline: str | None = None) -> dict:
    message = {
        'audioMessage': {
            'mimetype': 'audio/ogg; codecs=opus',
            'seconds': segundos,
            'ptt': True,
        },
    }
    if base64_inline is not None:
        message['base64'] = base64_inline
    return {
        'event': 'messages.upsert',
        'data': {
            'key': {
                'remoteJid': f'{TELEFONE}@s.whatsapp.net',
                'fromMe': False,
                'id': '3EB0AUDIO123',
            },
            'pushName': 'Cliente Teste',
            'message': message,
            'messageType': 'audioMessage',
        },
    }


class FakeEvolution:
    """Substitui notificacoes._post_com_retry: guarda os envios e serve o áudio."""

    def __init__(self, falhar_download: bool = False):
        self.falhar_download = falhar_download
        self.downloads: list[dict] = []
        self.textos: list[tuple[str, str]] = []  # (número, texto)

    async def __call__(self, url, *, json, headers, **kwargs):
        if '/chat/getBase64FromMediaMessage/' in url:
            self.downloads.append(json)
            if self.falhar_download:
                raise httpx.ConnectError('Evolution fora do ar')
            return httpx.Response(200, json={
                'mediaType': 'audioMessage',
                'mimetype': 'audio/ogg; codecs=opus',
                'base64': base64.b64encode(AUDIO_FALSO).decode(),
            })
        if '/sendText/' in url:
            self.textos.append((json['number'], json['text']))
        return httpx.Response(200, json={})

    def para_cliente(self) -> list[str]:
        return [t for n, t in self.textos if n == TELEFONE]

    def para_grupo(self) -> list[str]:
        return [t for n, t in self.textos if n == GRUPO]


def _cliente_openai(transcricao: str | None = None, erro: Exception | None = None):
    criar = AsyncMock()
    if erro is not None:
        criar.side_effect = erro
    else:
        criar.return_value = SimpleNamespace(text=transcricao)
    return SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=criar)))


class ExtrairDadosAudioTests(TestCase):
    def test_nota_de_voz_vira_interacao_audio(self):
        self.assertEqual(
            _extrair_dados_webhook(_payload_audio()),
            (TELEFONE, '[áudio]', 'audio'),
        )

    def test_audio_enviado_pelo_bot_e_ignorado(self):
        payload = _payload_audio()
        payload['data']['key']['fromMe'] = True
        self.assertIsNone(_extrair_dados_webhook(payload))


@override_settings(EVOLUTION_INSTANCE='robopod', AUDIO_DURACAO_MAXIMA_SEGUNDOS=120)
class WebhookAudioTests(TestCase):
    def setUp(self):
        self.evolution = FakeEvolution()
        self.processar = AsyncMock(
            return_value=(LLMResult(tipo='texto', conteudo='Anotado! Algo mais?'), []),
        )
        patches = [
            patch('core.notificacoes._post_com_retry', self.evolution),
            patch('core.views.processar_mensagem', self.processar),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _openai(self, **kwargs):
        cliente = _cliente_openai(**kwargs)
        p = patch('core.llm_service._get_client', return_value=cliente)
        p.start()
        self.addCleanup(p.stop)
        return cliente.audio.transcriptions.create

    async def _enviar(self, payload: dict):
        resp = await self.async_client.post(
            '/webhook/evolution/', data=payload, content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    async def _sessao(self, estado=SessaoChat.Estado.ATENDIMENTO_LLM, **campos):
        return await SessaoChat.objects.acreate(
            telefone=TELEFONE,
            estado_atual=estado,
            historico_mensagens=[{'role': 'assistant', 'content': 'Olá!'}],
            carrinho_temporario=campos.pop('carrinho', {}),
            **campos,
        )

    async def _historico_cliente(self) -> list[str]:
        return [
            m.texto async for m in Mensagem.objects.filter(
                telefone=TELEFONE, autor=Mensagem.Autor.CLIENTE,
            )
        ]

    # ── Sucesso ──

    async def test_audio_transcrito_segue_para_a_ia(self):
        await self._sessao()
        transcrever = self._openai(transcricao='Quero duas essências de menta')

        self.assertEqual(await self._enviar(_payload_audio()), {'status': 'ok'})

        # Baixou o áudio pela Evolution, pelo id da mensagem
        self.assertEqual(self.evolution.downloads, [
            {'message': {'key': {'id': '3EB0AUDIO123'}}, 'convertToMp4': False},
        ])
        # Transcreveu em português, com o arquivo e o formato certos
        kwargs = transcrever.await_args.kwargs
        self.assertEqual(kwargs['language'], 'pt')
        self.assertEqual(kwargs['file'], ('audio.ogg', AUDIO_FALSO))
        # A IA recebeu o texto como se o cliente tivesse digitado
        self.assertEqual(
            self.processar.await_args.kwargs['mensagem_usuario'],
            'Quero duas essências de menta',
        )
        self.assertEqual(self.evolution.para_cliente(), ['Anotado! Algo mais?'])
        self.assertEqual(
            await self._historico_cliente(),
            ['🎤 [áudio] Quero duas essências de menta'],
        )

    async def test_usa_base64_do_payload_sem_baixar(self):
        await self._sessao()
        transcrever = self._openai(transcricao='Tem carvão de coco?')

        await self._enviar(_payload_audio(base64_inline=base64.b64encode(AUDIO_FALSO).decode()))

        self.assertEqual(self.evolution.downloads, [])
        self.assertEqual(transcrever.await_args.kwargs['file'], ('audio.ogg', AUDIO_FALSO))
        self.assertEqual(self.processar.await_args.kwargs['mensagem_usuario'], 'Tem carvão de coco?')

    async def test_endereco_por_audio(self):
        await self._sessao(
            SessaoChat.Estado.AGUARDANDO_ENDERECO,
            carrinho={'itens': [{'nome': 'Carvão', 'quantidade': 1, 'preco_unitario': 20.0}]},
        )
        self._openai(transcricao='Rua das Flores, 120, Centro, Campinas.')

        with patch('core.views._task_calcular_frete', new=AsyncMock()) as task_frete:
            await self._enviar(_payload_audio())

        task_frete.assert_called_once_with(TELEFONE, 'Rua das Flores, 120, Centro, Campinas.')
        sessao = await SessaoChat.objects.aget(telefone=TELEFONE)
        self.assertEqual(sessao.estado_atual, SessaoChat.Estado.CALCULANDO_FRETE)

    async def test_comando_falado_cancelar(self):
        await self._sessao(SessaoChat.Estado.AGUARDANDO_ENDERECO, carrinho={'itens': []})
        self._openai(transcricao='Cancelar.')

        await self._enviar(_payload_audio())

        sessao = await SessaoChat.objects.aget(telefone=TELEFONE)
        self.assertEqual(sessao.estado_atual, SessaoChat.Estado.ATENDIMENTO_LLM)
        self.assertIn('Pedido cancelado', self.evolution.para_cliente()[0])

    async def test_atendimento_humano_repassa_transcricao_ao_grupo(self):
        await Configuracao.objects.aupdate_or_create(
            pk=1, defaults={'grupo_avisos_id': GRUPO, 'avisos_ativos': True},
        )
        await self._sessao(SessaoChat.Estado.ATENDIMENTO_HUMANO)
        self._openai(transcricao='Já fiz o pix, pode conferir?')

        await self._enviar(_payload_audio())

        self.assertEqual(len(self.evolution.para_grupo()), 1)
        self.assertIn(
            f'{TELEFONE}: 🎤 Já fiz o pix, pode conferir?', self.evolution.para_grupo()[0],
        )
        self.processar.assert_not_awaited()

    # ── Reserva quando não dá para transcrever ──

    async def _assert_reserva(self, trecho: str):
        self.processar.assert_not_awaited()
        respostas = self.evolution.para_cliente()
        self.assertEqual(len(respostas), 1)
        self.assertIn(trecho, respostas[0])
        self.assertEqual(await self._historico_cliente(), ['🎤 [áudio não transcrito]'])
        # A sessão não muda de estado
        sessao = await SessaoChat.objects.aget(telefone=TELEFONE)
        self.assertEqual(sessao.estado_atual, SessaoChat.Estado.ATENDIMENTO_LLM)

    async def test_falha_no_download(self):
        await self._sessao()
        self.evolution.falhar_download = True
        transcrever = self._openai(transcricao='não deveria chegar aqui')

        await self._enviar(_payload_audio())

        transcrever.assert_not_awaited()
        await self._assert_reserva('Não consegui entender seu áudio')

    async def test_falha_na_transcricao(self):
        await self._sessao()
        self._openai(erro=RuntimeError('OpenAI fora do ar'))

        await self._enviar(_payload_audio())

        await self._assert_reserva('Não consegui entender seu áudio')

    async def test_transcricao_vazia(self):
        await self._sessao()
        self._openai(transcricao='   ')

        await self._enviar(_payload_audio())

        await self._assert_reserva('Não consegui entender seu áudio')

    async def test_audio_longo_nao_e_transcrito(self):
        await self._sessao()
        transcrever = self._openai(transcricao='não deveria chegar aqui')

        await self._enviar(_payload_audio(segundos=300))

        self.assertEqual(self.evolution.downloads, [])
        transcrever.assert_not_awaited()
        await self._assert_reserva('áudio ficou um pouco longo')

    async def test_falha_em_atendimento_humano_avisa_o_grupo(self):
        await Configuracao.objects.aupdate_or_create(
            pk=1, defaults={'grupo_avisos_id': GRUPO, 'avisos_ativos': True},
        )
        await self._sessao(SessaoChat.Estado.ATENDIMENTO_HUMANO)
        self._openai(erro=RuntimeError('OpenAI fora do ar'))

        await self._enviar(_payload_audio())

        self.assertIn('não transcrito', self.evolution.para_grupo()[0])
        self.assertIn('Não consegui entender seu áudio', self.evolution.para_cliente()[0])

    async def test_falha_com_atendente_respondendo_nao_manda_reserva(self):
        await Configuracao.objects.aupdate_or_create(
            pk=1, defaults={'grupo_avisos_id': GRUPO, 'avisos_ativos': True},
        )
        await self._sessao(SessaoChat.Estado.ATENDIMENTO_HUMANO)
        await Mensagem.objects.acreate(
            telefone=TELEFONE, autor=Mensagem.Autor.ATENDENTE, texto='Oi, já te ajudo',
        )
        self._openai(erro=RuntimeError('OpenAI fora do ar'))

        await self._enviar(_payload_audio())

        self.assertEqual(len(self.evolution.para_grupo()), 1)
        self.assertEqual(self.evolution.para_cliente(), [])

    async def test_cliente_bloqueado_nao_gasta_transcricao(self):
        await Cliente.objects.acreate(telefone=TELEFONE, bloqueado=True)
        transcrever = self._openai(transcricao='oi')

        self.assertEqual(await self._enviar(_payload_audio()), {'status': 'cliente bloqueado'})

        transcrever.assert_not_awaited()
        self.assertEqual(self.evolution.textos, [])
        self.assertEqual(await self._historico_cliente(), ['🎤 [áudio]'])
