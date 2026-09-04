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
sudo apt install python3-gi gir1.2-webkit2-4.1 libgtk-3-0 xvfb python3.12-venv
# venv com acesso aos pacotes Python do sistema (python3-gi):
python3 -m venv .venv --system-site-packages
source .venv/bin/activate
pip install -r backend/requirements.txt   # instala pywebview==6.2.1 (sem extra)
# alternativa com uv (reproduzida): 
#   uv venv .venv --system-site-packages --python /usr/bin/python3.12
#   uv pip install -r backend/requirements.txt --index-strategy unsafe-best-match
#   (--index-strategy por causa do --extra-index-url do torch no lock)
```

Sem display (`$DISPLAY` vazio) o shell termina com exit code 1 e um
`Gtk-WARNING: cannot open display` no stderr — sem fallback
silencioso; a mensagem vem do GTK, não do shell.

## Reproduzir a validação (smoke)

O smoke abre a janela real em `file://` e verifica, com probes
`evaluate_js`:

**Confiança do shell (#334):**

1. resolução do build (dist + página de smoke);
2. janela carrega o build local via `file://`;
3. a **chat UI real** renderiza (header, input, send, empty state);
4. round-trip JS→Python→JS (`health()`, api_version 2);
5. o badge "desktop v2" do ChatHeader (round-trip visível na UI);
6. input inválido é rejeitado com erro sanitizado;
7. navegação remota (`https://example.com/`) é revertida para o build;
8. a bridge recusa chamadas com a janela em URL remota (fail-closed);
9. nenhum servidor HTTP escuta no processo (nenhuma porta);
10. fechar a janela encerra o shell (recursos liberados).

**Runtime local #336 (mesma execução):**

11. `runtime_state()` pela bridge reporta armazenamento local pronto;
12. um turno completo via `send_message()` (provider scriptado offline,
    sem cota Groq, sem rede) comita localmente;
13. o turno está durável no SQLite — provado por uma conexão
    `sqlite3` **independente e read-only** (não o caminho de leitura
    do runtime);
14. um runtime **novo** sobre o mesmo arquivo (o que um relançamento
    é) recupera a conversa e a revisão persistida;
15. a operação local de privacidade `delete_history()` apaga de
    verdade (0 linhas em `chat_logs` depois, verificação
    independente).

**Caminho de aceitação do usuário (#336, mesmo smoke):**

16. envio REAL pela UI: digitar no textarea → clicar em enviar →
    ChatWindow renderiza a resposta (useChat → transport → bridge →
    runtime → SQLite, sem chamar a bridge diretamente);
17. privacidade REAL pela UI: clicar "Apagar histórico" → confirmação
    explícita → status "Operação concluída" renderizado (o painel
    real, não chamadas diretas de bridge).

**Prova do provider real (bloqueio registrado):** o smoke usa provider
scriptado porque a rede Groq não é determinística nem gratuita. A perna
de rede real com chave Groq não é executada neste ambiente (nenhuma
chave configurada — `runtime_state().provider_configured == false`); o
comportamento sem chave foi verificado no caminho de produção: o app
abre, lê histórico, e o primeiro turno retorna o erro sanitizado
`configuration` ("O provedor remoto não está configurado neste
ambiente.") sem traceback, path ou segredo.

```bash
# build do frontend (produz dist/index.html e dist/desktop-smoke.html)
cd frontend && npm ci && npm run build && cd ..

# smoke headless
xvfb-run -a -s "-screen 0 1280x800x24" \
  .venv/bin/python scripts/desktop_smoke.py
```

Sucesso: todas as linhas `[PASS]` e `SMOKE_OK` no final.

### Nota sobre o runtime do smoke (#336)

O smoke usa um banco descartável (`katherine-smoke-336-*`) e um
provider scriptado offline: o banco real do usuário nunca é tocado e
nenhuma cota Groq é gasta. O ciclo de vida exercitado é o de produção
(bridge → runtime → LocalStorage → SQLite). A prova de persistência
não confia na leitura do próprio runtime: uma conexão `sqlite3`
independente (read-only) inspeciona o arquivo.

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

## Pacote `.deb` do desktop (#338)

O pacote Debian reutiliza exatamente o shell `backend.desktop.app` e o
build de produção do Vite. O layout instalado é:

- `/usr/bin/katherine`: wrapper que fixa o `sys.path` na árvore instalada;
- `/usr/lib/katherine/backend`: somente o fechamento de imports do runtime
  desktop;
- `/usr/lib/katherine/frontend/dist`: `desktop.html` e os assets do build;
- `/usr/lib/katherine/vendor`: dependências Python desktop travadas em
  `packaging/requirements-desktop.txt`;
- `/usr/share/applications/katherine.desktop`: atalho do menu.

Não há servidor HTTP, daemon, serviço systemd ou uma segunda implementação
do runtime. O pacote depende do Python e do stack nativo PyGObject,
WebKitGTK 4.1, GTK 3 e GLib do sistema. As dependências Python puras são
desempacotadas no diretório `vendor`; FastAPI, Supabase, Uvicorn, PyTorch e
NumPy não entram no pacote.

### Build e instalação

O build exige que o frontend já esteja pronto e baixa somente as versões do
lock desktop:

```bash
cd frontend && npm ci && npm run build && cd ..
SOURCE_DATE_EPOCH=1700000000 \
  python3 packaging/build_deb.py \
  --version 0.1.0 \
  --out-dir dist/deb
```

Em uma instalação Debian/Ubuntu, instale primeiro as dependências nativas
listadas no metadata do pacote, depois instale o arquivo gerado:

```bash
sudo apt install python3 python3-gi gir1.2-webkit2-4.1 \
  libwebkit2gtk-4.1-0 libgtk-3-0 libglib2.0-0
sudo dpkg -i dist/deb/katherine-desktop_0.1.0_all.deb
sudo apt-get -f install
katherine
```

O build e a instalação nunca criam nem copiam um banco de dados de usuário.
Em runtime, o arquivo fica em `~/.local/share/katherine/katherine.db`.
Também não há arquivo de configuração em `/etc` nem `conffile`, portanto
upgrade, remove e purge não devem apagar dados do usuário.

### Upgrade, rollback e migrações

Um upgrade normal substitui somente a árvore do pacote:

```bash
sudo dpkg -i katherine-desktop_0.2.0_all.deb
```

O rollback é a instalação do `.deb` anterior:

```bash
sudo dpkg -i katherine-desktop_0.1.0_all.deb
```

O banco não é revertido durante nenhum desses comandos. O runner de
migrações aplica cada versão dentro de uma transação e só grava a versão
depois do sucesso. Se uma versão mais nova estiver registrada no banco, uma
versão antiga do app falha fechada com `schema_too_new`, sem resetar,
recriar ou abrir silenciosamente um schema desconhecido.

### Evidência de ciclo de vida real

`packaging/isolated-install.sh` cria um namespace de usuário, mount e PID,
usa `pivot_root`, executa o `dpkg` real e mantém um banco `status` separado.
Ele não usa Docker, mocka o dpkg ou toca o banco do host. O teste completo é:

```bash
python3 packaging/smoke_deb.py \
  --deb dist/deb/katherine-desktop_0.1.0~test2_all.deb \
  --old-deb dist/deb/katherine-desktop_0.1.0~test1_all.deb
```

Na execução validada, o resultado foi `ALL PASS (7/7)`:

1. instalação e configuração `dpkg`;
2. import fora do checkout e resolução do frontend instalado;
3. criação do banco XDG no primeiro uso, com schema 1;
4. turno sem chave com erro sanitizado `configuration`;
5. upgrade para a versão nova;
6. downgrade para a versão anterior com o banco reaberto e preservado;
7. purge, confirmação de que os arquivos do pacote sumiram, reinstalação e
   confirmação independente de uma linha sentinela no mesmo `katherine.db`.

A aceitação gráfica do pacote instalado usa a janela GTK/WebKit real, não a
página de smoke:

```bash
packaging/gui_smoke_deb.sh \
  dist/deb/katherine-desktop_0.1.0~test2_all.deb
```

Esse probe abre a entrada `/usr/bin/katherine` em Xvfb, espera a janela
Katherine, digita e envia uma mensagem pela UI real, mede os descendentes do
shell, verifica que não há sockets TCP em LISTEN e fecha a janela. No caso
sem chave, o turno retornou o erro de configuração esperado. A execução
validada terminou com `clean_exit=true`, `exit_code=0`, nenhum processo
remanescente e nenhum listener TCP.

### Medição do pacote (#338)

Ambiente da medição: Linux Mint 22.3, kernel 7.0.0-30-generic, x86_64,
Python 3.12.3, PyGObject 3.48.2, WebKitGTK 2.52.3, Node.js 22.23.2,
12 CPUs. O pacote foi construído com `SOURCE_DATE_EPOCH=1700000000`.
Uma reconstrução do mesmo commit e versão produziu o mesmo SHA-256:
`2f44175ca70d98876bd28c8ef692652a84693f74308c33b5e0550e07b0e0f82d`.

Os números abaixo são uma execução real do `GUI_RESULTS`, em Xvfb
1280x800 com o app instalado, após o carregamento inicial. RSS e CPU são a
soma dos processos descendentes observados; idle foi amostrado por 5 s e o
turno por 3 s. Eles são evidência do ambiente acima, não limites de
aceitação:

| Medida | Resultado |
| --- | ---: |
| `.deb` | 3,147,830 bytes (3,074 KiB) |
| `Installed-Size` Debian | 12,488 KiB |
| Árvore instalada (`du -sk`) | 13,756 KiB |
| Startup até janela GTK | 1,033.2 ms |
| RSS idle, descendentes | 603,156 KiB |
| CPU idle, janela de 5 s | 0.0% |
| Pico RSS durante turno | 638,204 KiB |
| CPU durante turno, janela de 3 s | 19.45% |
| Shutdown após fechar a janela | 113.8 ms |

A variação de startup e CPU entre execuções depende da carga do host e do
backend WebKitGTK. Por isso o projeto registra os números observados e o
método, sem transformar esta amostra em um threshold artificial.

## Modelo de confiança da bridge (resumo)

1. `make_js_api()` (api.py) entrega só a allowlist `DESKTOP_API_METHODS`
   — o contrato v2 (`DESKTOP_API_VERSION = 2`) com **8 métodos**:
   `health`, `runtime_state`, `load_history`, `send_message` e as
   quatro operações de privacidade (`delete_history`,
   `delete_memories`, `reset_emotional_state`,
   `reset_relationship_state`) — todos com erros sanitizados; nenhum
   método exposto levanta. O frontend faz feature-check pelo número
   `api_version` (não fareja métodos); todo método valida seus
   argumentos antes de tocar o runtime e devolve payloads estruturados
   sem traceback, path, SQL ou conteúdo ecoado.
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
