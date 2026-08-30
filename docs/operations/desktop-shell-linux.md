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

O backend GTK do pywebview precisa do stack GObject/WebKit via PyGObject:

- **PyGObject ≥ 3.48** (o venv usa `--system-site-packages` no Ubuntu
  24.04; o PyGObject do sistema é usado, ele não é instalável via pip
  sem build deps nativos)
- WebKit2GTK 4.1 (`libwebkit2gtk-4.1-0`, `libwebkit2gtk-4.1-dev` ao
  construir PyGObject)
- GTK 3 e GLib (`libgtk-3-0`, `libglib2.0-0`)
- Xvfb para execução headless (apenas validação/CI):
  `xvfb-run -a -s "-screen 0 1280x800x24"`

Instalação recomendada do lado Python (requirements.in já inclui):

```
pywebview[gtk]
```

O extra `[gtk]` garante a seleção do backend GTK no Linux. Sem
display (`$DISPLAY` vazio) o shell não sobe — esse é um erro
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
reverte imediatamente). O smoke aceita qualquer resultado seguro:
a chamada remota foi recusada com o payload sanitizado
`bridge_unavailable`, o documento morreu antes de obter a bridge
(nenhuma chamada chegou ao Python), ou a chamada morreu junto com o
documento (nenhum payload foi entregue). O único resultado que falha
é um payload bem-sucedido entregue a conteúdo remoto.

## Medidas (Ubuntu 24.04, WebKitGTK, headless via Xvfb)

- Startup até página carregada: ~550-650ms
- RSS idle: ~175MB (pico durante load: ~200MB)
- CPU idle: 1-2% (0% em steady state)
- Processos: 1 (nenhum servidor/worker extra)

## Modelo de confiança da bridge (resumo)

1. `make_js_api()` (api.py) entrega só a allowlist `DESKTOP_API_METHODS`
   (`health`) com erros sanitizados; nenhum método exposto levanta.
2. `LocalBuildBridge` (app.py) só serve chamadas quando a página atual
   é exatamente a URL local cujo load **completou** (trust commitido
   no evento `loaded`). Durante qualquer navegação em curso —
   remota, ou o próprio revert — a bridge recusa (`bridge_unavailable`).
3. Navegação para fora do build é revertida pelo handler `loaded`.

Detalhes e racional da corrida revert (get_uri reflete o load
iniciado, não o documento vivo) estão em comentários no
`backend/desktop/app.py` (classe `BuildTrust`).
