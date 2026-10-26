"""Painel como app no celular (PWA): manifest, service worker, ícones e a
inscrição nas notificações.

Com isso, "Adicionar à Tela de Início" no iPhone (ou "Instalar app" no
Android/Chrome) abre o painel em tela cheia, sem a barra do navegador.
Manifest, service worker e ícones são públicos: o celular busca esses
arquivos sem o cookie de login. Nada neles expõe dados da loja.
"""

import json
from pathlib import Path

from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from . import auditoria, push
from .auditoria import Tipo
from .models import InscricaoPush

PASTA_ICONES = Path(__file__).resolve().parent / 'pwa'
ICONES = {'icone-180', 'icone-192', 'icone-512', 'favicon'}


@require_GET
def manifest(request):
    inicio = reverse('painel:inicio')
    icone = lambda nome: reverse('painel:pwa_icone', args=[nome])  # noqa: E731
    dados = {
        'id': inicio,
        'name': 'Robopod · Painel da loja',
        'short_name': 'Robopod',
        'lang': 'pt-BR',
        'start_url': inicio,
        'scope': inicio,
        'display': 'standalone',
        'background_color': '#f4f4f2',
        'theme_color': '#fcfcfb',
        'icons': [
            {'src': icone('icone-192'), 'sizes': '192x192', 'type': 'image/png'},
            {'src': icone('icone-512'), 'sizes': '512x512', 'type': 'image/png'},
            # O robô cabe na área segura, então o mesmo desenho serve de ícone "recortável"
            {'src': icone('icone-512'), 'sizes': '512x512', 'type': 'image/png', 'purpose': 'maskable'},
        ],
    }
    resposta = JsonResponse(dados, content_type='application/manifest+json',
                            json_dumps_params={'ensure_ascii': False})
    resposta['Cache-Control'] = 'public, max-age=86400'
    return resposta


@require_GET
def icone(request, nome):
    if nome not in ICONES:
        raise Http404
    resposta = FileResponse(open(PASTA_ICONES / f'{nome}.png', 'rb'), content_type='image/png')
    resposta['Cache-Control'] = 'public, max-age=604800'
    return resposta


# Não guarda páginas do painel no celular (têm dados de clientes): só mostra um
# aviso amigável quando o painel não abre, que quase sempre é o Tailscale desligado.
SERVICE_WORKER = r"""
const SEM_CONEXAO = `<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Sem conexão · Robopod</title>
<style>
:root { color-scheme: light dark; }
body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px;
  font: 15px/1.5 system-ui, -apple-system, sans-serif; background: #f4f4f2; color: #0b0b0b; text-align: center; }
@media (prefers-color-scheme: dark) { body { background: #121211; color: #fff; } p { color: #c3c2b7; } }
h1 { font-size: 20px; margin: 0 0 8px; } p { margin: 0 0 20px; color: #52514e; max-width: 320px; }
button { font: inherit; font-weight: 600; border: 0; border-radius: 8px; padding: 10px 20px; background: #2a78d6; color: #fff; }
</style></head>
<body><div>
<h1>Não foi possível abrir o painel</h1>
<p>Confira se o Tailscale está ligado neste celular e se há internet. Depois toque em tentar de novo.</p>
<button onclick="location.reload()">Tentar de novo</button>
</div></body></html>`;

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (evento) => evento.waitUntil(self.clients.claim()));

self.addEventListener('fetch', (evento) => {
  if (evento.request.mode !== 'navigate') return;
  evento.respondWith(fetch(evento.request).catch(() =>
    new Response(SEM_CONEXAO, { headers: { 'Content-Type': 'text/html; charset=utf-8' } })));
});

// Notificação vinda do serviço `notificacoes` (painel/push.py)
self.addEventListener('push', (evento) => {
  let d = {};
  try { d = evento.data ? evento.data.json() : {}; } catch (e) {}
  const tarefas = [self.registration.showNotification(d.titulo || 'Robopod', {
    body: d.corpo || '',
    tag: d.tag || undefined,
    renotify: !!d.tag,  // mesmo pedido/conversa substitui o aviso anterior, mas toca de novo
    icon: ICONE,
    badge: ICONE,
    data: { url: d.url || INICIO },
  })];
  // Número no ícone do app (iPhone com iOS 16.4+, Android)
  if (typeof d.contador === 'number' && self.navigator.setAppBadge) {
    tarefas.push((d.contador ? self.navigator.setAppBadge(d.contador) : self.navigator.clearAppBadge()).catch(() => {}));
  }
  evento.waitUntil(Promise.all(tarefas));
});

// Tocar na notificação abre o painel direto no pedido ou na conversa
self.addEventListener('notificationclick', (evento) => {
  evento.notification.close();
  const url = new URL(evento.notification.data?.url || INICIO, self.location.origin).href;
  evento.waitUntil((async () => {
    const janelas = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const janela of janelas) {
      try {
        await janela.focus();
        await janela.navigate(url);
        return;
      } catch (e) {}
    }
    await self.clients.openWindow(url);
  })());
});
"""


@require_GET
def service_worker(request):
    constantes = (f'const INICIO = {json.dumps(reverse("painel:inicio"))};\n'
                  f'const ICONE = {json.dumps(reverse("painel:pwa_icone", args=["icone-192"]))};\n')
    resposta = HttpResponse(constantes + SERVICE_WORKER, content_type='text/javascript; charset=utf-8')
    # Sem cache: assim uma versão nova do service worker chega logo aos celulares
    resposta['Cache-Control'] = 'no-cache'
    return resposta


# ── Inscrição nas notificações (seção "Notificações neste aparelho" em Configurações) ──

def _aparelho(request) -> str:
    ua = request.META.get('HTTP_USER_AGENT', '')
    for chave, nome in (('iPhone', 'iPhone'), ('iPad', 'iPad'), ('Android', 'Android'),
                        ('Windows', 'Windows'), ('Macintosh', 'Mac'), ('Linux', 'Linux')):
        if chave in ua:
            return nome
    return ''


def _ler_json(request) -> dict:
    try:
        dados = json.loads(request.body or b'{}')
    except ValueError:
        return {}
    return dados if isinstance(dados, dict) else {}


def _inscricao_do_pedido(request, dados) -> InscricaoPush | None:
    endpoint = str(dados.get('endpoint') or '')
    return InscricaoPush.objects.filter(endpoint=endpoint, usuario=request.user).first() if endpoint else None


@login_required
@require_POST
def push_salvar(request):
    """Cria ou atualiza a inscrição deste aparelho (e os eventos escolhidos)."""
    if not push.ativo():
        return JsonResponse({'erro': 'Notificações não configuradas no servidor'}, status=503)
    dados = _ler_json(request)
    inscricao = dados.get('inscricao') or {}
    endpoint = str(inscricao.get('endpoint') or '')
    chaves = inscricao.get('keys') or {}
    if not push.endpoint_valido(endpoint) or not chaves.get('p256dh') or not chaves.get('auth'):
        return JsonResponse({'erro': 'Inscrição inválida'}, status=400)

    existente = InscricaoPush.objects.filter(endpoint=endpoint).first()
    eventos = dados.get('eventos')
    if isinstance(eventos, list):
        eventos = [e for e in eventos if e in push.EVENTOS]
    else:
        # Só sincronizando (a página abriu): mantém o que o aparelho já tinha
        eventos = existente.eventos if existente else push.EVENTOS_PADRAO
    # O mesmo aparelho pode trocar de usuário: a inscrição passa para quem está logado
    obj, criada = InscricaoPush.objects.update_or_create(endpoint=endpoint, defaults={
        'usuario': request.user,
        'p256dh': str(chaves['p256dh'])[:200],
        'auth': str(chaves['auth'])[:100],
        'eventos': eventos,
        'aparelho': _aparelho(request),
    })
    if criada:
        auditoria.registrar(request, Tipo.CONFIGURACAO, 'notificacoes_ativar',
                            f'Ativou as notificações no aparelho ({obj.aparelho or "desconhecido"})')
    return JsonResponse({'eventos': obj.eventos})


@login_required
@require_POST
def push_cancelar(request):
    inscricao = _inscricao_do_pedido(request, _ler_json(request))
    if inscricao:
        inscricao.delete()
        auditoria.registrar(request, Tipo.CONFIGURACAO, 'notificacoes_desativar',
                            f'Desativou as notificações no aparelho ({inscricao.aparelho or "desconhecido"})')
    return JsonResponse({'ok': True})


@login_required
@require_POST
def push_testar(request):
    inscricao = _inscricao_do_pedido(request, _ler_json(request))
    if not inscricao or not push.ativo():
        return JsonResponse({'ok': False}, status=404)
    ok = push.enviar(inscricao, {
        'titulo': 'Teste do Robopod', 'corpo': 'As notificações estão funcionando neste aparelho.',
        'url': reverse('painel:configuracoes') + '#notificacoes', 'tag': 'teste',
    })
    return JsonResponse({'ok': ok})
