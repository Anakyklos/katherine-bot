# Supabase Security Upgrade — Adoção e Recuperação Forward-Only

## Visão geral

Duas migrations definem o schema do banco:

| Migration | Propósito |
|---|---|
| `20240101000000_baseline.sql` | Schema inicial (tabelas, índices, FKs, RPC) |
| `20240101000002_secure_server_owned_tables.sql` | Hardening de segurança (RLS, FORCE RLS, grants, constraints, default privileges) |

O hardening é irreversível por design. Não existe rollback da migration de segurança.

---

## 1. Instalação vazia (banco novo)

```bash
supabase db reset
```

O `db reset` aplica todas as migrations na ordem do timestamp, produzindo:

- RLS + FORCE RLS ativos nas quatro tabelas sensíveis (`profiles`, `chat_logs`, `memories`, `archival_extractions`)
- Apenas `service_role` com privilégios de tabela (SELECT, INSERT, UPDATE, DELETE)
- Sem políticas de row-level para clientes (`anon`, `authenticated`)
- Constraints de validação em `chat_logs` (role, tamanho do content)
- `match_memories` executável apenas por `service_role`, com `SECURITY INVOKER`

---

## 2. Instalação legado existente

### 2.1 A baseline não é destrutiva

A migration baseline (`20240101000000`) usa `create table if not exists` e `create extension if not exists`. Ela pode ser aplicada sobre tabelas existentes sem perda de dados.

### 2.2 Validação pré-hardening

Antes de aplicar a migration de hardening, **valide os dados existentes**:

```sql
-- Verificar linhas incompatíveis com as constraints que serão adicionadas
SELECT count(*) AS invalid_role FROM chat_logs WHERE role NOT IN ('user', 'assistant');
SELECT count(*) AS empty_content FROM chat_logs WHERE char_length(content) = 0 OR content IS NULL;
SELECT count(*) AS long_content FROM chat_logs WHERE char_length(content) > 10000;
```

Se alguma contagem for > 0, a migration de hardening **falhará** com SQLSTATE `23514`.

### 2.3 Aplicação do hardening

```bash
# Se a baseline já estiver registrada, aplique apenas o hardening:
supabase migration up --local
```

Isso registra o timestamp `20240101000002` em `supabase_migrations.schema_migrations` e aplica todas as alterações de segurança.

### 2.4 Verificação pós-hardening

```bash
supabase test db supabase/tests/database
```

Espere:

- 63 assertions pgTAP passando
- RLS e FORCE RLS confirmados nas quatro tabelas
- Grants exatos para `anon`, `authenticated`, `PUBLIC` e `service_role`
- Privilégios de sequence limitados a `USAGE` para `service_role`
- `match_memories` executável apenas por `service_role`

---

## 3. Dados incompatíveis

Se a migration de hardening falhar devido a dados incompatíveis em `chat_logs`:

1. **A migration não altera nem apaga registros.** O erro ocorre dentro de uma transação que é revertida.
2. O timestamp da migration **não** é registrado como aplicado.
3. O banco permanece no estado anterior (baseline apenas, sem RLS, sem constraints).

### Correção manual necessária

```sql
-- Identificar as linhas problemáticas
SELECT id, user_id, role, char_length(content) AS content_len
FROM chat_logs
WHERE role NOT IN ('user', 'assistant')
   OR char_length(content) = 0
   OR content IS NULL
   OR char_length(content) > 10000;

-- Corrigir (exemplo: atualizar role inválida)
UPDATE chat_logs SET role = 'user' WHERE role NOT IN ('user', 'assistant');

-- Ou remover as linhas (se apropriado)
DELETE FROM chat_logs WHERE char_length(content) = 0;
```

Após a correção, a migration de hardening pode ser aplicada novamente:

```bash
supabase migration up --local
```

---

## 4. Recuperação forward-only

### Regras

Depois que o hardening for aplicado com sucesso:

- ❌ **Não apague** a migration `20240101000002_secure_server_owned_tables.sql`.
- ❌ **Não reverta** o hardening. As alterações são destrutivas por design.
- ❌ **Não desabilite** RLS ou FORCE RLS.
- ❌ **Não restaure** grants para `anon` ou `authenticated`.
- ❌ **Não recrie** políticas de leitura direta (como a antiga "Users can select their own archival extractions").
- ❌ **Não use** `git revert`, drop da migration ou `supabase migration repair` para reverter o hardening.

### Correção de defeitos

Qualquer defeito no schema de segurança deve ser corrigido por **uma nova migration forward-only** que:

1. Preserve ou fortaleça a fronteira de autorização existente.
2. Seja numerada com timestamp posterior (ex.: `20250101000003_...`).
3. Seja testada via pgTAP antes do merge.

### Exemplo

```sql
-- 20250101000003_fix_policy_name.sql
-- Corrige nome de constraint sem desabilitar RLS.
ALTER TABLE public.chat_logs RENAME CONSTRAINT chat_logs_role_check TO chat_logs_valid_role_check;
```

---

## 5. Drift legado: `public.rls_auto_enable()` — decisão PRESERVE + VERSION + HARDEN

### Origem

A auditoria do projeto Supabase hospedado encontrou a função
`public.rls_auto_enable()` **não versionada** pelas migrations do repositório.
Após a reativação do projeto, a inspeção do objeto real confirmou que ele está
**ativo** e exerce uma função de segurança real:

- `public.rls_auto_enable()`: schema `public`, zero argumentos, retorno
  `event_trigger`, `LANGUAGE plpgsql`, `SECURITY DEFINER`, owner `postgres`,
  `search_path = pg_catalog`;
- event trigger real `ensure_rls`: evento `ddl_command_end`, habilitado, tags
  `CREATE TABLE`, `CREATE TABLE AS`, `SELECT INTO`, apontando para
  `public.rls_auto_enable()`;
- a função tenta habilitar RLS automaticamente em tabelas novas do schema
  `public`;
- ACL observada: `{=X/postgres, postgres=X/postgres, anon=X/postgres,
  authenticated=X/postgres, service_role=X/postgres}` (EXECUTE amplo);
- o corpo legado usa SQL dinâmico e um `EXCEPTION WHEN OTHERS` que engole
  qualquer falha e grava apenas um log genérico.

A origem exata do objeto **não foi localizada no histórico versionado
disponível**. Por isso o objeto é tratado como drift legado de origem não
comprovada. Não há afirmação de origem conhecida.

### Risco

`SECURITY DEFINER` executa com os privilégios do owner. `EXECUTE` concedido a
`PUBLIC`/roles de runtime em uma função privilegiada amplia a superfície de
ataque. Além disso, o corpo legado com SQL dinâmico e `WHEN OTHERS` pode
deixar uma tabela nova desprotegida silenciosamente. O Database Advisor do
Supabase reportava `anon_security_definer_function_executable` e
`authenticated_security_definer_function_executable`.

### Decisão: PRESERVE + VERSION + HARDEN

A função e o event trigger passam a fazer parte do schema **versionado e
reproduzível** do projeto. Um `supabase db reset` novo termina com a mesma
versão canônica do mecanismo que um banco legado atualizado. O mecanismo não é
removido nesta tarefa.

A migration `20260807201256_harden_rls_auto_enable.sql`:

1. **Cria/substitui** `public.rls_auto_enable()` (zero argumentos, retorno
   `event_trigger`, `LANGUAGE plpgsql`, `SECURITY DEFINER`,
   `SET search_path = pg_catalog`) com o corpo canônico:
   - resolve a relação criada por OID via catálogos
     (`pg_catalog.pg_event_trigger_ddl_commands()` + `pg_class`/
     `pg_namespace`);
   - cobre `CREATE TABLE`, `CREATE TABLE AS` e `SELECT INTO` em `public`
     (tabelas comuns e particionadas);
   - monta o `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` com identificadores
     escapados via `pg_catalog.format('%I.%I', ...)`;
   - **fail-closed**: sem `WHEN OTHERS` para transformar falha em sucesso
     silencioso; se o RLS não puder ser habilitado, o erro propaga e o DDL
     falha;
   - não usa texto arbitrário/entrada do usuário em SQL dinâmico.
2. **Versiona o event trigger** canônico `ensure_rls`:
   - se ausente, cria com evento `ddl_command_end`, tags `CREATE TABLE`,
     `CREATE TABLE AS`, `SELECT INTO` e função `public.rls_auto_enable()`;
   - se presente, valida o estado (função, evento, tags); estado inesperado
     falha explicitamente; trigger desabilitado é reabilitado;
   - nunca deixa dois event triggers apontando para a função — triggers
     duplicados legados são removidos.
3. **Revoga** `EXECUTE` de `PUBLIC`, `anon`, `authenticated` e `service_role`.
   Nenhuma outra role recebe EXECUTE. O owner `postgres` permanece responsável
   pela função.

### Comportamento por cenário

| Cenário | Resultado |
|---|---|
| Banco limpo (`supabase db reset`) | Função canônica e trigger `ensure_rls` criados; grants de runtime revogados |
| Upgrade legado (objeto existe) | Corpo legado convergido para a definição canônica; trigger reconciliado; grants de runtime revogados |
| Reavaliação da migration | Sem falha; não recria grants; não duplica função/trigger; estado canônico inalterado |

### Validação no catálogo

```sql
-- Função canônica (clean reset e legacy upgrade convergem para o mesmo estado)
SELECT n.nspname, p.proname, p.pronargs, p.prorettype::regtype::text,
       p.prosecdef, p.proowner::regrole::text, p.proconfig
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public' AND p.proname = 'rls_auto_enable' AND p.pronargs = 0;

-- Event trigger canônico
SELECT et.evtname, et.evtevent, et.evtfoid = p.oid AS same_function,
       et.evtenabled, et.evttags
FROM pg_event_trigger et
JOIN pg_proc p ON p.oid = et.evtfoid
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE et.evtname = 'ensure_rls';

-- Nenhuma role de runtime mantém EXECUTE; owner mantém
SELECT has_function_privilege('public',         'public.rls_auto_enable()', 'EXECUTE') AS pub,
       has_function_privilege('anon',           'public.rls_auto_enable()', 'EXECUTE') AS anon,
       has_function_privilege('authenticated',  'public.rls_auto_enable()', 'EXECUTE') AS authenticated,
       has_function_privilege('service_role',   'public.rls_auto_enable()', 'EXECUTE') AS service_role,
       has_function_privilege('postgres',       'public.rls_auto_enable()', 'EXECUTE') AS owner;
```

### Advisor

Após a migration, o advisor local não reporta nenhum alerta relacionado a
`rls_auto_enable` (incluindo `anon/authenticated_security_definer_
function_executable`):

```bash
supabase db advisors --local --type security --output-format json
```

### Remoção definitiva futura

Qualquer remoção definitiva da função/event trigger deve:

1. partir de evidência de que o mecanismo é obsoleto (nenhum fluxo legado
   depende dele);
2. ser tratada em issue separada com migration própria e testes de upgrade;
3. ser registrada nesta documentação antes da execução.

---

## 6. Testes de integração

### Sequência determinística (CI)

```bash
supabase start
supabase db reset
supabase test db supabase/tests/database          # pgTAP (425 assertions, 5 arquivos)

# Upgrade legado válido
python -m pytest -q -ra backend/tests/test_legacy_upgrade.py

supabase db reset                                   # estado limpo para auth tests

# Matriz PostgREST
python -m pytest -q -ra backend/tests/test_database_authorization_integration.py

# Drift legado de public.rls_auto_enable() (#291)
supabase db reset
bash scripts/hide-migrations-after.sh 20240101000006 \
  python -m pytest -q -ra backend/tests/test_rls_auto_enable_legacy.py

supabase stop
```

### Backend (offline, sem Supabase)

```bash
python -m pytest backend/tests \
  --ignore=backend/tests/test_database_authorization_integration.py \
  --ignore=backend/tests/test_legacy_upgrade.py \
  --ignore=backend/tests/test_rls_auto_enable_legacy.py
```

Inclui `test_memory_configuration.py` que testa sanitização de chaves e exceções sem dependência de rede.
