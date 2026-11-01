# Benchmark local do Kokoro-82M em português brasileiro

**Issues:** [#370](https://github.com/Anakyklos/Katherine-bot/issues/370), [#332](https://github.com/Anakyklos/Katherine-bot/issues/332)

**Conclusão:** **INCONCLUSIVE**
**Execuções completas:** 3 × 20 frases, CPU, 1º de novembro de 2026 (duas medições iniciais e uma reexecução após a correção P1)

## Decisão

A síntese real local está comprovada neste host: o Kokoro-82M v1.0 com a voz `pf_dora` gerou 20 WAVs válidos em cada uma de três execuções completas, usando a fonemização `pt-br` e PyTorch CPU. O RTF agregado ficou abaixo de 1 nas três execuções.

O resultado é **INCONCLUSIVE** porque nenhuma pessoa avaliou as amostras. Os arquivos comprovam formato, duração, finitude e ausência de clipping amostrado; não comprovam inteligibilidade, pronúncia brasileira adequada, naturalidade, timbre agradável ou ausência perceptiva de cortes e distorções. Além disso, não foi aprovado um limite de memória ou latência para o desktop. Este relatório não recomenda integração nem substitui a decisão do mantenedor.

## Candidato e proveniência

| Item | Valor |
| --- | --- |
| Repositório do modelo | [`hexgrad/Kokoro-82M`](https://huggingface.co/hexgrad/Kokoro-82M) |
| Release / revisão imutável | v1.0 / `f3ff3571791e39611d31c381e3a41a3af07b4987` |
| Voz / idioma | `pf_dora` / `pt-br` (`lang_code="p"`) |
| Inferência | CPU; PyTorch `2.13.0+cpu`; 4 threads |
| Taxa de saída | 24.000 Hz, mono PCM_16 WAV |
| Parâmetros declarados pelo runtime | 81.810.022; esse número não é usado como estimativa de RAM |
| Licença do modelo e vozes | Apache-2.0, conforme o [model card](https://huggingface.co/hexgrad/Kokoro-82M) |

Os tamanhos e SHA-256 abaixo são verificados pelo harness antes de cada execução. O manifesto versionado está em [`model_artifacts.json`](../../benchmarks/kokoro_ptbr/model_artifacts.json).

| Arquivo | Bytes | SHA-256 |
| --- | ---: | --- |
| `config.json` | 2.351 | `5abb01e2403b072bf03d04fde160443e209d7a0dad49a423be15196b9b43c17f` |
| `kokoro-v1_0.pth` | 327.212.226 | `496dba118d1a58f5f3db2efc88dbdc216e0483fc89fe6e47ee1f2c53f18ad1e4` |
| `voices/pf_dora.pt` | 523.425 | `07e4ff987c5d5a8c3995efd15cc4f0db7c4c15e881b198d8ab7f67ecf51f5eb7` |

O código de inferência foi instalado como `kokoro==0.9.4` ([PyPI](https://pypi.org/project/kokoro/0.9.4/)); o conjunto de dependências, versões e hashes das distribuições está isolado em [`uv.lock`](../../benchmarks/kokoro_ptbr/uv.lock). O caminho PT-BR executado foi Misaki `0.9.4` (`EspeakG2P`) com `phonemizer-fork==3.3.2`, `espeakng-loader==0.2.4` e biblioteca eSpeak NG `1.52.0`. O `phonemizer-fork` declara GNU GPL em seus metadados; eSpeak NG é GPL-3.0-or-later. Esses componentes pertencem somente ao benchmark. A compatibilidade técnica foi comprovada pela execução, mas qualquer integração futura exige revisão própria de licenças.

Não foram testados pesos quantizados, GPU, Pocket TTS ou fallback remoto.

## Corpus e método

O corpus [`corpus.jsonl`](../../benchmarks/kokoro_ptbr/corpus.jsonl) contém 20 frases sintéticas versionadas, sem conversas reais, gravações ou dados privados. Ele cobre três respostas muito curtas, saudações, despedida, perguntas, expressividade, preços, horários, data, nomes, siglas, inglês misturado, termos técnicos e frases longas. Uma pré-verificação de G2P produziu fonemas não vazios para os 20 itens; a maior sequência tinha 185 caracteres, abaixo do limite de 510 usado pelo Kokoro.

O harness baixa arquivos somente com o subcomando explícito `fetch-assets --allow-downloads`, usa revisão fixada, desativa token Hugging Face e mantém cache e logs dentro do diretório passado. `run` verifica hashes localmente, desliga rede no worker e não tenta baixar pesos. O benchmark não abre dispositivo de áudio nem inicia serviço persistente. Cada WAV é escrito atomicamente e validado; mensagens de falha são códigos e textos fixos, sem traceback ou caminho de usuário.

As medidas de inicialização e WAV usam relógio monotônico. CPU por frase e ocioso usa o relógio de CPU do processo; RSS, PSS (`smaps_rollup`) e CPU adicional são amostrados em `/proc` a cada 100 ms. O percentual de CPU é normalizado com 100% equivalente a um núcleo lógico; portanto, uso paralelo pode exceder 100%. Picos amostrados podem perder transientes menores que o intervalo. Os relatórios medem o processo separado do benchmark, não o app com WebKitGTK simultaneamente.

“Cold start” aqui significa processo novo, incluindo inicialização do Python e imports. O cache de arquivos do sistema não foi limpo; portanto, não é uma medição de cold I/O do disco. A mediana warm exclui a primeira frase e reúne frases de comprimentos diferentes. RTF é tempo total de G2P + inferência dividido pela duração dos WAVs; escrita do WAV não entra no RTF agregado.

## Ambiente

| Item | Medição |
| --- | --- |
| Sistema | Linux Mint 22.3; kernel `7.0.0-30-generic`; x86_64 |
| CPU | Intel Core i5-1235U, 10 núcleos / 12 CPUs lógicas |
| RAM física reportada pelo host | 31 GiB |
| Python / uv | Python `3.12.3`; uv `0.12.5` |
| RAM disponível antes da síntese | aproximadamente 18 GiB (`free -h`) |
| Disco disponível antes da síntese | aproximadamente 42 GiB (`df -h /home/pedro`) |

## Resultados de desempenho

As duas execuções completas foram concluídas com exit code 0, 20/20 amostras e sem arquivos parciais. Ambas geraram 83,825 segundos de áudio. Os intervalos abaixo são os resultados medidos em `full-01` e `full-02`; a diferença entre rodadas impede tratar um único tempo como valor estável.

| Métrica | Execução 1 | Execução 2 | Leitura |
| --- | ---: | ---: | --- |
| Processo iniciado → modelo pronto | 3.204,8 ms | 3.265,6 ms | Inclui inicialização/imports |
| Construção/carga do modelo | 638,3 ms | 649,2 ms | Após imports |
| Processo iniciado → primeiro WAV pronto | 5.690,9 ms | 5.731,5 ms | Primeira amostra `Sim.` |
| Primeira geração após modelo carregado | 481,4 ms | 443,7 ms | G2P + inferência da primeira frase |
| Mediana warm de geração por frase | 2.014,6 ms | 2.363,8 ms | Exclui a primeira frase |
| Duração de áudio / tempo total G2P + inferência | 83,825 s / 48,047 s | 83,825 s / 52,805 s | 20 frases |
| RTF agregado / throughput | 0,573 / 1,745× | 0,630 / 1,587× | Abaixo de RTF 1 nas duas execuções |
| CPU médio durante geração | 371,9% | 370,6% | Aproximadamente 3,7 CPUs lógicas ocupadas |
| Ocioso solicitado com modelo carregado | 2 s | 2 s | Voz e modelo permaneceram carregados |
| Ocioso: CPU do relógio direto do processo | 0,0001 s / 0,005% | 0,0001 s / 0,005% | O bucket do sampler `/proc` divergiu (9,8% e 3,1%); a estimativa de CPU idle fica **INCONCLUSIVE** por atribuição de fase/intervalo |
| Shutdown event → saída do worker | 761,1 ms | 732,7 ms | Worker encerrou com exit code 0 |
| Processo iniciado → saída | 54,240 s | 59,038 s | Inclui as 20 frases e idle de 2 s |

As três respostas curtas (`Sim.`, `Entendi.`, `Claro!`) geraram WAVs de 0,75–1,125 s. Seus RTFs medianos foram 0,603 na primeira e 0,570 na segunda rodada. Entre as frases longas, o RTF ficou entre 0,638–0,657 na primeira rodada e 0,649–0,674 na segunda. *(O detalhamento por frase está em cada `metrics.json` local.)*

### Memória do processo

| Medida | Execução 1 | Execução 2 |
| --- | ---: | ---: |
| RSS pico / incremental sobre baseline do worker | 1.936 MiB / 1.935 MiB | 2.027 MiB / 2.026 MiB |
| PSS pico / incremental sobre baseline do worker | 1.925 MiB / 1.924 MiB | 2.016 MiB / 2.015 MiB |
| PSS pico amostrado no idle com modelo carregado | 1.006 MiB | 1.006 MiB |

O snapshot do desktop instalado documentado em [`desktop-shell-linux.md`](../operations/desktop-shell-linux.md) mede PSS agregado de 348.577 KiB em idle e 396.229 KiB durante turno de UI. O PSS incremental deste worker separado ficou em cerca de 5,0–5,2 vezes o pico PSS do turno de UI. É uma comparação de escala entre medições documentadas no mesmo perfil de host, não uma execução simultânea ou um orçamento de aceitação. A soma de memória do app + TTS não foi medida e não deve ser inferida como valor de integração.

Os aproximadamente 82 milhões de parâmetros não predizem esse consumo: o PSS pico observado nas três execuções completas ficou entre 1,88 e 2,05 GiB. As duas medições iniciais também mostram que frases mais longas elevam o pico amostrado.

### Disco e saída

| Diretório / artefato | Evidência |
| --- | ---: |
| Arquivos fixados de modelo/voz | 327.738.002 bytes aparentes (312,6 MiB) |
| Cache Hugging Face | 655.476.195 bytes aparentes no JSON; `du -sh`: 313 MiB alocados |
| Cache uv isolado | 1.220.423.414 bytes aparentes; `du -sh`: 1,3 GiB alocados |
| Ambiente benchmark `.venv` | 1.288.778.176 bytes aparentes contando arquivos apontados; `du -sh`: 85 MiB alocados no caminho da venv |
| WAVs de uma rodada completa | 4.024.480 bytes aparentes; arquivos PCM_16 mono 24 kHz |

As medidas “aparentes” somam comprimentos de arquivos e podem contar destinos de links mais de uma vez; `du -sh` relata blocos alocados pelo caminho medido. O cache de pesos, uv, ambiente e WAVs ficou fora do Git. A síntese não fez downloads. Ao final, o host ainda tinha aproximadamente 41 GiB disponíveis.

## Qualidade e dispositivo de áudio

Todos os 40 WAVs das duas execuções passaram validações estruturais, tinham amplitudes finitas e apresentaram fração de clipping amostrada igual a zero. Isso não detecta pronúncia inadequada, cortes percebidos, ruído, prosódia estranha ou distorção audível. O benchmark detectou nós de áudio e `paplay`, `pw-play` e `aplay`, mas não reproduziu os arquivos. A avaliação humana segue **UNVERIFIED**. A geração de WAV não depende do dispositivo de áudio; a ausência do nó de áudio foi coberta por teste do probe, sem desligar o hardware do host.

As amostras ficaram fora do repositório. Caminho no host medido:

- `/home/pedro/.cache/katherine-kokoro-benchmark/runs/full-01/`
- `/home/pedro/.cache/katherine-kokoro-benchmark/runs/full-02/`

Para escuta manual, começar por `short_sim.wav`, `short_entendi.wav`, `short_claro.wav`, `number_price.wav`, `mixed_english.wav` e `long_companion.wav`. A classificação não muda sem escuta efetiva.

## Falhas, interrupção e recuperação

- **Pesos ausentes/corrompidos:** testes unitários cobrem ausência e hash incorreto; execução falha antes de abrir o worker. A aquisição exige a opção explícita `--allow-downloads`.
- **Configuração e argumentos inválidos:** testes cobrem ausência de opções obrigatórias, modelo/revisão/voz não fixados, corpus duplicado/inválido e texto acima do limite.
- **Fonemização/inferência/WAV:** erros artificiais em testes confirmam códigos e mensagens limitados, sem propagar detalhes internos; WAV inválido e falha de escrita limpam o temporário.
- **Dispositivo de áudio ausente:** o probe sem `/dev/snd` reporta ausência, e síntese não requer playback.
- **Interrupção real durante síntese:** Ctrl+C foi enviado ao processo Python depois de 6 WAVs completos. O comando retornou 130 com `error_code=interrupted`; o JSON registrou 7 amostras concluídas, worker encerrado (`exit_code=1`), `persistent_process_after_exit=false` e `partial_files_remaining=false`. Os WAVs completos e o `metrics.json` foram preservados; nenhum worker permaneceu.
- **Limite do teste por timeout externo:** uma tentativa via GNU `timeout` entregou o sinal também ao wrapper `uv` e terminou com código 124 antes de persistir `metrics.json`; não deixou WAV parcial nem worker. O resultado de interrupção citado acima usa Ctrl+C pelo terminal, que é o caminho de uso avaliado.
- **Encerramento normal:** nas duas execuções completas o worker encerrou com código 0, sem serviço ou processo persistente e sem arquivos parciais.

## Reprodução

```bash
CACHE_ROOT="$HOME/.cache/katherine-kokoro-benchmark"

uv sync --project benchmarks/kokoro_ptbr --locked \
  --cache-dir "$CACHE_ROOT/uv-cache"

uv run --project benchmarks/kokoro_ptbr --locked \
  --cache-dir "$CACHE_ROOT/uv-cache" \
  python -m pytest benchmarks/kokoro_ptbr/tests -q

uv run --project benchmarks/kokoro_ptbr --locked \
  --cache-dir "$CACHE_ROOT/uv-cache" \
  python benchmarks/kokoro_ptbr/benchmark.py fetch-assets \
  --model-id hexgrad/Kokoro-82M \
  --model-revision f3ff3571791e39611d31c381e3a41a3af07b4987 \
  --voice pf_dora --model-dir "$CACHE_ROOT/assets" \
  --cache-dir "$CACHE_ROOT/huggingface" --allow-downloads

uv run --project benchmarks/kokoro_ptbr --locked \
  --cache-dir "$CACHE_ROOT/uv-cache" \
  python benchmarks/kokoro_ptbr/benchmark.py run \
  --model-id hexgrad/Kokoro-82M \
  --model-revision f3ff3571791e39611d31c381e3a41a3af07b4987 \
  --voice pf_dora --model-dir "$CACHE_ROOT/assets" \
  --corpus benchmarks/kokoro_ptbr/corpus.jsonl \
  --output-dir "$CACHE_ROOT/runs/reproduced" \
  --cache-dir "$CACHE_ROOT/huggingface" \
  --uv-cache-dir "$CACHE_ROOT/uv-cache" --threads 4
```

Os `metrics.json` medidos ficaram em `diagnostic-01`, `diagnostic-02`, `full-01`, `full-02` e `interrupted-02` dentro de `$CACHE_ROOT/runs`. O harness não salva WAVs nem pesos no checkout.

## Testes e limites de aceite

- Harness isolado: **31 passed** após a correção P1 da PR #371; sintaxe verificada por `py_compile`.
- `uv lock --project benchmarks/kokoro_ptbr --check`: passou.
- `uv lock --project backend --check`: passou; nenhum arquivo de lock do backend foi alterado.
- Backend, comando unitário do CI com os módulos de banco real explicitamente excluídos: **2.985 passed, 1 warning**, em 27,83 s.
- `git diff --check`: registrado na entrega da PR.
- A execução exploratória sem exclusões do CI coletou testes de integração de banco fora do ambiente Supabase: **2.985 passed, 25 failed, 210 errors**; as falhas ficaram nos módulos DB-only/preflight que o workflow do CI exclui. A execução final acima usa o filtro oficial do job unitário; nenhuma variável `SUPABASE_URL` estava configurada.

### Verificação da correção P1 da PR #371

A revisão apontou que `completed` não garantia cobertura integral do corpus nem consistência dos WAVs. A classificação agora exige contagem exata, IDs completos e únicos, acordo com `synthesized_count`, e existência, hash, cabeçalho, dados PCM e metadados consistentes para cada WAV. Inconsistências produzem erro fixo e sanitizado; `metrics.json` conserva as contagens, IDs divergentes e diagnóstico por artefato.

- Testes negativos cobrem amostra ausente, ID duplicado, divergência da contagem do worker e WAV ausente ou corrompido; todos confirmam `status=failed` e evidência de validação preservada em `metrics.json`.
- Inferência real em CPU após a correção: corpus completo de 20 frases, saída externa em `$CACHE_ROOT/runs/p1-validation-full-01`, pelo comando `run` de reprodução acima sem `--limit`, `--threads 4`, modelo `hexgrad/Kokoro-82M` na revisão `f3ff3571791e39611d31c381e3a41a3af07b4987` e voz `pf_dora`. Assets já verificados; a execução não baixou artefatos.
- `metrics.json` registrou `status=completed`, contagens esperada/eventos/worker de **20/20/20**, IDs sem ausência, duplicação ou excedentes e 20/20 WAVs íntegros. Cabeçalho/dados e metadados passaram em todos; hashes SHA-256 individuais foram preservados nos registros de cada artefato no próprio `metrics.json`.
- A reexecução gerou 83,825 s de áudio em 61,892 s de G2P + inferência (RTF 0,738; throughput 1,354× realtime). O pico amostrado foi 2.106 MiB RSS e 2.094 MiB PSS, incluindo baseline inferior a 1 MiB; primeiro WAV pronto em 6.718 s, encerramento do worker após shutdown em 982 ms e nenhum arquivo parcial ou processo persistente.
- A execução completa comprova o caminho corrigido para o corpus integral. Ainda não houve avaliação auditiva; a conclusão de qualidade segue **INCONCLUSIVE**.

Não há critério aprovado de RAM, latência, qualidade subjetiva ou custo de instalação para declarar o candidato apto. Não classificar como PASS sem avaliação auditiva e decisão do mantenedor. Nenhum limite novo foi criado neste experimento.

## Escopo, risco e rollback

O risco medido mais relevante é a memória standalone: aproximadamente 1,9–2,1 GiB PSS de pico nas três execuções completas e cerca de 1,0 GiB PSS no idle carregado nas duas medições iniciais. A avaliação de qualidade brasileira ainda depende de escuta. A distribuição transitive GPL também requer análise antes de eventual integração no produto.

O trabalho adiciona somente uma ferramenta, corpus, lock isolado, testes e este relatório. `ConversationEngine`, `LanguageModel`, backend de produção, locks do backend, bundle React, SQLite, sistema emocional, microfone/STT, empacotamento e interface desktop não foram alterados. Não há migração. Rollback consiste em reverter esta PR; os arquivos de modelo, cache e áudio são externos ao repositório e podem ser removidos separadamente pelo mantenedor conforme a política local.
