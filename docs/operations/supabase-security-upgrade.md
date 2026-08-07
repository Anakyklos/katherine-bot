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

## 5. Drift legado: `public.rls_auto_enable()` — decisão PRESERVE_AND_HARDEN

### Origem

Uma auditoria do projeto Supabase hospedado encontrou a função
`public.rls_auto_enable()` **não versionada** pelas migrations do repositório,
com as seguintes características:

- `SECURITY DEFINER`, owner `postgres`, retorno `event_trigger`,
  `search_path=pg_catalog`;
- `EXECUTE` concedido a `PUBLIC`, `anon`, `authenticated` e `service_role`;
- um event trigger associado (mecanismo legado que habilita RLS em tabelas
  novas).

Isso cria drift entre o ambiente hospedado e o schema versionado: um banco
limpo nunca recebe a função, mas o ambiente legado a expõe como função
privilegiada com grants de execução amplos.

### Risco

`SECURITY DEFINER` executa com os privilégios do owner. `EXECUTE` concedido a
`PUBLIC`/roles de runtime em uma função privilegiada amplia a superfície de
ataque sem necessidade funcional documentada.

### Decisão: PRESERVE_AND_HARDEN

A migration `20260807201256_harden_rls_auto_enable.sql`:

- identifica o objeto **exatamente** pelo catálogo: schema `public`, nome
  `rls_auto_enable`, zero argumentos, retorno `event_trigger`;
- revoga `EXECUTE` apenas de `PUBLIC`, `anon`, `authenticated` e
  `service_role`;
- **não** remove, recria, altera o corpo/owner/search_path da função;
- **não** remove nem altera o event trigger associado;
- é um no-op em banco limpo (objeto ausente) e idempotente.

### Comportamento por cenário

| Cenário | Resultado |
|---|---|
| Banco limpo (`supabase db reset`) | Migration aplica normalmente; a função e o event trigger não existem |
| Upgrade legado (função existe) | Função e event trigger preservados; os quatro grants de `EXECUTE` são removidos |
| Reavaliação do bloco de hardening | Sem falha; não recria grants; não duplica nem altera objetos |

### Por que não remover o event trigger nesta entrega

A remoção definitiva do mecanismo (função + event trigger) exige evidência de
que ele é obsoleto em todos os ambientes, decisão de arquitetura e issue
separada. Esta PR apenas elimina a exposição às roles de runtime, preservando
o comportamento do mecanismo legado quando ele existir.

### Validação no catálogo

```sql
-- Função preservada com a identidade original
SELECT n.nspname, p.proname, p.pronargs, p.prorettype::regtype::text,
       p.prosecdef, p.proowner::regrole::text
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public' AND p.proname = 'rls_auto_enable' AND p.pronargs = 0;

-- Event trigger continua associado à mesma função
SELECT et.evtname, et.evtfoid, et.evtfoid = p.oid AS same_function
FROM pg_event_trigger et
JOIN pg_proc p ON p.oid = et.evtfoid
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public' AND p.proname = 'rls_auto_enable';

-- Nenhuma role de runtime mantém EXECUTE
SELECT has_function_privilege('public',         'public.rls_auto_enable()', 'EXECUTE') AS pub,
       has_function_privilege('anon',           'public.rls_auto_enable()', 'EXECUTE') AS anon,
       has_function_privilege('authenticated',  'public.rls_auto_enable()', 'EXECUTE') AS authenticated,
       has_function_privilege('service_role',   'public.rls_auto_enable()', 'EXECUTE') AS service_role;
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
supabase test db supabase/tests/database          # pgTAP (407 assertions, 5 arquivos)

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
