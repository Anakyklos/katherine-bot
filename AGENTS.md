# AGENTS.md — Katherine Bot

Este arquivo define as regras obrigatórias para agentes de código, incluindo Jules.

## Autoridade e fluxo

- O mantenedor/auditor define arquitetura, prioridade, escopo e critérios de aceite.
- O agente implementa somente a tarefa recebida e entrega a mudança por pull request.
- Sempre crie uma branch nova a partir da `main` atualizada e abra a PR contra `main`.
- Nunca crie PR empilhada sobre a branch de outra PR. Se uma dependência ainda não foi mesclada, declare o bloqueio no comentário inicial e não implemente sobre ela.
- Uma tarefa gera no máximo uma PR. Não recrie automaticamente uma PR fechada e não duplique trabalho existente.
- O Jules não deve depender de comentários posteriores, threads ou pedidos de alteração na PR. Correções após a abertura serão enviadas como uma nova tarefa explícita pelo mantenedor.

## Escopo

- Não faça melhorias paralelas, refatorações oportunistas ou alterações cosméticas fora da tarefa.
- Não edite `.Jules/palette.md` salvo quando a tarefa pedir explicitamente.
- Preserve contratos públicos e dados persistidos; qualquer quebra exige migração e registro na PR.
- Mudanças no sistema emocional devem separar: percepção/appraisal, transição de estado, persistência, relacionamento e apresentação.
- Estado de usuário nunca pode ficar em singleton global ou ser compartilhado entre requisições.

## Segurança e produto

- Nunca confie em `user_id` enviado pelo cliente sem validar a identidade autenticada.
- Não adicione segredos ao repositório, logs ou corpo da PR.
- Não introduza instruções de engano sobre a natureza do sistema, manipulação emocional, coerção ou sexualização por padrão.
- Dados emocionais e memórias são dados sensíveis: aplique minimização, isolamento por usuário e autorização.

## Qualidade mínima

Antes de abrir a PR:

1. Execute os testes relevantes e registre os comandos e resultados.
2. Adicione ou atualize testes para toda regra de domínio alterada.
3. Verifique estados limite, concorrência e falhas de integração quando aplicável.
4. Mantenha a PR pequena e revisável; divida tarefas maiores em etapas independentes.
5. Não deixe `print` de depuração, código morto, TODO sem issue ou tratamento genérico que esconda erros.

## Formato da PR

O comentário inicial/corpo da PR deve conter:

- Issue/tarefa de origem.
- Problema e causa raiz.
- Solução e decisões tomadas.
- Arquivos/áreas afetadas.
- Testes executados e resultado.
- Riscos, migração e rollback.
- Itens deliberadamente fora de escopo.

Use Conventional Commits e títulos como `feat(emotion): ...`, `fix(auth): ...` ou `test(emotion): ...`.

## Liberação de issues

- Somente issues explicitamente liberadas pelo mantenedor podem receber
  branch ou pull request.
- Nenhuma PR concluída autoriza iniciar outra issue automaticamente; a
  próxima tarefa precisa de liberação explícita do mantenedor.

## Tooling Python (workflow `uv`)

O backend Python usa `uv` como fonte autoritativa de dependências.

- A autoridade do grafo é `backend/pyproject.toml` + `backend/uv.lock`.
- Provisionar o ambiente: `uv sync --project backend` (CI usa `--frozen`,
  que falha se o lock divergir e nunca o reescreve).
- Executar comandos Python: sempre `uv run --project backend ...`
  (testes: `uv run --project backend python -m pytest backend/tests`).
- Adicionar dependência: `uv add "pkg==x.y.z"` (runtime) ou
  `uv add --group test "pkg==x.y.z"` (teste).
- Remover dependência: `uv remove "pkg"` (ou `uv remove --group test "pkg"`).
- Atualizar o lock conscientemente: `uv lock` e depois regenerar o export
  de compatibilidade do Docker:
  `uv export --frozen --no-emit-project --no-hashes --emit-index-url --no-group test --output-file requirements.txt`.
- Verificar o lock sem modificá-lo: `uv lock --check`.
- Não use `pip install` no Python global, `pip-compile`, `pip-tools` nem
  `python -m venv` para o fluxo normal do backend. `backend/requirements.txt`
  é um artefato GERADO (export do lock) consumido apenas pelo Docker;
  nunca o edite à mão.
- PyTorch permanece CPU-only: o índice do PyTorch é configurado como
  `explicit` e só o `torch` roteia para ele. Não introduza GPU como
  requisito.
- O pacote desktop (`packaging/requirements-desktop.*`) tem lock
  deliberadamente separado e mínimo; não o una ao grafo do backend nem
  faça o `.deb` exigir `uv` na máquina do usuário.
