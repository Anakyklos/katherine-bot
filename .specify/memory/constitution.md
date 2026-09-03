# Katherine Bot Constitution

Princípios que governam toda mudança no backend e no desktop companion.

## Core Principles

### I. Contrato único por fronteira
Cada fronteira de infraestrutura (persistência, LLM, auth) tem um único
contrato canônico, derivado dos call sites reais. Nada de abstrações
universais especulativas, `**kwargs`, ou objetos de SDK atravessando a
fronteira. Implementações concretas vivem atrás de um adapter explícito.

### II. Domínio isento de provedor (Test-First)
O núcleo de domínio (turn flow, appraisal, transição, política confiável)
nunca importa símbolos de provedor remoto. Toda regra de domínio é testável
com um fake determinístico injetado na costura do contrato. Testes não são
enfraquecidos: mudança de regra exige teste novo ou atualizado.

### III. Sanitização em todas as superfícies (NÃO NEGOCIÁVEL)
Segredos (chaves de API, tokens) existem apenas no lado Python: nunca no
bundle React, retornos da bridge, `repr`, logs, nem mensagens de erro.
Erros cruzam fronteiras como códigos de baixa cardinalidade com mensagens
constantes. Nada de texto de exceção bruto, prompt, conteúdo do usuário ou
detalhe de infraestrutura vaza para cima.

### IV. Local-first no desktop
O desktop abre sem login, sem Supabase e sem provedor remoto configurado.
Nenhuma requisição de provedor em idle/startup, sem threads de provedor em
background. Criação de adapter é lazy (primeiro uso). Histórico SQLite é
legível sem provedor. Sem regressão de atomicidade/replay, allowlist da
bridge, timeouts/cancelamento e modo web.

### V. Falha explícita, sem fallback silencioso
Seleção de provedor/modelo é explícita. Sem auto-routing, sem fallback para
outros provedores. Falha do provedor é falha do turno, conforme o contrato.
Provedor desconhecido falha sanitizado. Exceções de SDK são traduzidas para
erros canônicos dentro do adapter, nunca propagadas cruas.

### VI. Escopo mínimo e verificável
Uma tarefa, uma branch a partir de `main` atualizada, uma PR. Sem melhorias
paralelas, refatorações oportunistas ou cosméticas fora do escopo. Estado de
usuário nunca em singleton global nem compartilhado entre requisições. Sem
dependência nova sem necessidade comprovada. Dados emocionais e memórias são
sensíveis: minimização e isolamento por usuário sempre.

## Quality Gates

1. `python -m compileall -q backend` passa.
2. Suíte de testes unitários do CI (sem integração Supabase) passa.
3. Frontend: `npm test`, `npm run lint`, `npm run build` passam quando o
   frontend é tocado.
4. Smoke headless do desktop (`scripts/desktop_smoke.py` sob `xvfb-run`)
   passa quando o caminho desktop é tocado.
5. A PR declara testes executados com números, riscos, migração/rollback e
   itens fora de escopo.
