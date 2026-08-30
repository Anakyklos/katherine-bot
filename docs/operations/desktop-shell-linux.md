# Desktop Shell no Linux (pywebview + WebKitGTK)

> Issue #334 — validação do pywebview como shell desktop Linux. Este
> documento descreve como executar o shell localmente no Linux, suas
> dependências de sistema e como reproduzir a validação (smoke).

## Visão geral

O shell desktop é uma janela nativa GTK (via `pywebview`) hospedando o
build de produção do frontend (Vite) direto por `file://`. Não existe
servidor HTTP para a UI: nenhum processo extra, nenhuma porta, nenhum
loopback. O único canal entre a página e o Python é a bridge
`js_api` do pywebview (ver `backend/desktop/app.py`).

- Entrada de produção: `python -m backend.desktop.app`
- Entrada de smoke: `python scripts/desktop_smoke.py` (abre
  `dist/desktop-smoke.html`, que monta a `ChatWindow` real)

## Dependências de sistema (Linux)

O backend GTK do pywebview precisa do stack GObject/WebKit via PyGObject.
O ambiente validado usa o PyGObject do **sistema**, não do pip:

- **PyGObject ≥ 3.48** — no Ubuntu 24.04 vem do pacote `python3-gi`
  (validado com 3.48.2). Ele não é instalável via pip sem build deps
  nativos e **não** faz parte do `requirements.in`/lock: a venv é
  criada com `--system-site-packages` e o importa de
  `/usr/lib/python3/dist-packages`.
- WebKit2GTK 4.1 (`libwebkit2gtk-4.1-0`; validado com 2.52.3 no
  Ubuntu 24.04) — typelib `gir1.2-webkit2-4.1`.
- GTK 3 e GLib (`libgtk-3-0`, `libglib2.0-0`).
- Xvfb para execução headless (apenas validação/CI):
  `xvfb-run -a -s "-screen 0 1280x800x24"`.

Instalação do lado Python: o `requirements.in` trava `pywebview==6.2.1`
(**sem** o extra `[gtk]`). O backend GTK é selecionado automaticamente
pelo pywebview no Linux quando o PyGObject do sistema e o WebKitGTK
estão disponíveis; o extra `[gtk]` do pip (que traria
`PyGObject==3.50.0` do PyPI) **não** é usado nem validado. Instalação
reproduzível do ambiente validado:

```bash
sudo apt install python3-gi gir1.2-webkit2-4.1 libgtk-3-0 xvfb
# venv com acesso aos pacotes Python do sistema (python3-gi):
python3 -m venv .venv --system-site-packages
source .venv/bin/activate
pip install -r backend/requirements.txt   # instala pywebview==6.2.1 (sem extra)
```

Sem display (`$DISPLAY` vazio) o shell não sobe — esse é um erro
explícito, não um fallback silencioso.

## Reproduzir a validação (smoke)

O smoke abre a janela real em `file://` e verifica, com probes
`evaluate_js`:

1. resolução do build (dist + página de smoke);
2. janela carrega o build local via `file://`;
3. a **chat UI real** renderiza (header, input, send, empty state);
4. round-trip JS→Python→JS (`health()`);
5. o badge "desktop v1" do ChatHeader (round-trip visível na UI);
6. input inválido é rejeitado com erro sanitizado;
7. navegação remota (`https://example.com/`) é revertida para o build;
8. a bridge recusa chamadas com a janela em URL remota (fail-closed);
9. nenhum servidor HTTP escuta no processo (nenhuma porta);
10. fechar a janela encerra o shell (recursos liberados).

```bash
# build do frontend (produz dist/index.html e dist/desktop-smoke.html)
cd frontend && npm ci && npm run build && cd ..

# smoke headless
xvfb-run -a -s "-screen 0 1280x800x24" \
  ../.venv/bin/python scripts/desktop_smoke.py
```

Sucesso: todas as linhas `[PASS]` e `SMOKE_OK` no final.

### Nota sobre o check "remote navigation"

O documento remoto vive por uma janela curta (o handler `loaded` o
reverte imediatamente). O smoke prova o isolamento em três estágios:

- **A** — página remota carregada normalmente: `health()` recusada
  (recusa executada no próprio handler `loaded`, a testemunha
  autoritária);
- **B** — revert emitido para a MESMA URL do entry: no instante em que
  o `load_url` do revert é chamado (documento remoto ainda vivo, novo
  load local ainda não completou, `get_uri()` já vai ler a mesma URL
  `file://`), `health()` continua recusada — igualdade de URL não
  reabre a bridge;
- **C** — novo load local completou: a bridge volta a servir o
  documento local.

O probe in-vivo (armado dentro do documento remoto vivo) é mantido
como evidência extra best-effort: disparar ou não depende do timing do
WebKitGTK (o doc remoto muitas vezes morre antes da injeção, o que em
si é um resultado seguro — nada foi entregue). A prova determinística
da máquina de estados same-URL (todas as transições do trust) está em
`backend/tests/test_desktop_navigation.py::TestRevertToSameEntryUrl`.

## Medidas (Ubuntu 24.04, WebKitGTK, headless via Xvfb)

- Startup até página carregada: ~550-650ms (do início do processo,
  incluindo resolução do build; janela do webview apenas: ~260-450ms)
- RSS idle: ~190MB estável (190148-190528 kB em amostragem de 2s por
  14s de janela viva); pico (VmHWM): ~190.5MB idle, ~195MB no smoke
  completo (194832-195316 kB em 4 runs, inclui a fase de navegação
  remota)
- CPU: 1-2% durante o load; **0% em steady state** (5 amostras de 2s
  consecutivas a 0% com a janela viva)
- Processos: 1 (nenhum servidor/worker extra)

## Modelo de confiança da bridge (resumo)

1. `make_js_api()` (api.py) entrega só a allowlist `DESKTOP_API_METHODS`
   (`health`) com erros sanitizados; nenhum método exposto levanta.
2. `LocalBuildBridge` (app.py) só serve chamadas quando a página atual
   é exatamente a URL local cujo load **completou** (trust commitido
   no evento `loaded`).
3. Navegação para fora do build é detectada no handler `loaded`, que
   **revoga o trust (`BuildTrust.revoke()`) ANTES de emitir o
   `load_url` de retorno**. Durante qualquer navegação em curso —
   remota, ou o próprio revert, inclusive quando `get_uri()` já voltou
   a ler a mesma URL do entry — a bridge recusa (`bridge_unavailable`),
   porque igualdade de URL não comprova nem identidade do documento
   nem conclusão do load. Somente o evento `loaded` do novo load
   local (novo commit) reabre a bridge.

Detalhes e racional da corrida revert (get_uri reflete o load
iniciado, não o documento vivo) estão em comentários no
`backend/desktop/app.py` (classes `BuildTrust` e `NavigationPolicy`).
