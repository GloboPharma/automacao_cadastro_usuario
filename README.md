# Automação de Cadastro de Usuários – Modelos de Autorização (SAP Business One)

Automação em Python que inclui **novos usuários** do SAP Business One como **Autores**
nos **Modelos de Autorização** (*Modelos de autorização – Definição*), conforme o
departamento do usuário.

## Regras de negócio

Os modelos em que cada usuário novo entra como Autor são definidos **por
departamento**, no arquivo `regras_autores.json`:

```json
{
  "default": null,
  "departments": {
    "4":  ["SC ", "BRSUPPLY", "CONSUMO", "CONT.", "NF ", "PV"],
    "43": ["PV"]
  }
}
```

- **Chave:** código do departamento (`OUDP.Code`).
- **Valor:** prefixos do **nome** do modelo de autorização. O usuário entra em todo
  modelo cujo nome começa com um dos prefixos, sem diferenciar maiúsculas. Use um
  espaço no fim quando necessário (`"SC "` não pega um modelo chamado `SCRAP...`).
- **`default`:** regra para departamentos não listados. Com `null`, o usuário **não
  é incluído em nenhum modelo**; o script registra um aviso por usuário e um resumo
  no fim do log, para tratamento manual.
- Chaves que começam com `_` são comentários e são ignoradas.

As regras iniciais foram derivadas da cobertura atual de autores em produção: os
usuários de um mesmo departamento costumam ser autores de toda uma família de
modelos (SC, BRSUPPLY, CONSUMO...) ou de nenhuma. Os códigos de departamento podem
variar entre bases; confira-os antes de usar em outro ambiente.

Regras adicionais:

- O **nome do usuário** (`OUSR.U_NAME` / `UserName`) é gravado sempre em **maiúsculas**.
  O código de login (`USER_CODE`) não é alterado.
- Apenas usuários **novos** são processados. O controle é feito pelo último
  `USERID` / `InternalKey` processado, salvo em um arquivo de estado **por base**.
- Usuários **bloqueados** são ignorados.
- Por padrão, só modelos **ativos** recebem autores (`ONLY_ACTIVE_TEMPLATES`).
- O script é **idempotente**: não duplica autores que já existem no modelo.

## Estrutura do projeto

```
automacao_cadastro_usuario/
├── main.py            # Versão via Service Layer (recomendada / produção)
├── main_hana.py       # Versão via conexão direta ao HANA (somente testes)
├── regras_autores.json          # Regras departamento -> modelos
├── regras_autores.example.json  # Modelo do arquivo de regras
├── .env               # Configuração e credenciais (NÃO versionar)
├── .env.example       # Modelo do .env, sem senhas
├── dados/             # Exportações de consultas (NÃO versionar)
├── .gitignore
├── requirements.txt
└── README.md
```

Arquivos gerados em tempo de execução (ignorados pelo Git):

| Arquivo | Descrição |
|---|---|
| `sync_autores.log` | Log do `main.py` |
| `sync_autores_hana.log` | Log do `main_hana.py` |
| `sync_autores_state_<CompanyDB>.json` | Último usuário processado pelo `main.py`, por base |
| `sync_autores_state_hana_<SCHEMA>.json` | Último usuário processado pelo `main_hana.py`, por schema |

O nome da base entra sempre no nome do arquivo de estado. Assim, ao apontar o
`.env` para outra base, a primeira execução nela apenas grava o ponto de partida,
sem reaproveitar o estado de outro ambiente.

## Qual script usar

| | `main.py` (Service Layer) | `main_hana.py` (HANA direto) |
|---|---|---|
| Suportado pela SAP | ✅ Sim | ❌ Não (escrita direta em tabelas) |
| Uso indicado | Produção e homologação | Somente homologação / validação da lógica |
| Credenciais | Usuário do **SAP B1** | Usuário do **banco HANA** |
| Tabelas / entidades | `Users`, `Departments`, `ApprovalTemplates` | `OUSR`, `OUDP`, `OWTM`, `WTM1` |
| Transação | Uma requisição por alteração | Transação única (commit / rollback) |

> ⚠️ **Atenção:** gravar diretamente nas tabelas do SAP Business One via SQL não é
> suportado pela SAP e pode comprometer o suporte. Use o `main_hana.py` apenas no
> ambiente de testes.

## Pré-requisitos

- Python 3.10 ou superior
- Acesso de rede ao Service Layer (`main.py`) ou à porta SQL do HANA (`main_hana.py`)
- Usuário do SAP B1 com permissão para alterar usuários e modelos de autorização
  (recomenda-se um usuário dedicado à integração)

## Instalação

```powershell
cd automacao_cadastro_usuario
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Conteúdo esperado do `requirements.txt` (dependências diretas):

```
requests
python-dotenv
hdbcli
```

## Configuração

Copie o modelo e preencha com os dados do ambiente:

```powershell
Copy-Item .env.example .env
```

### Service Layer (`main.py`)

| Variável | Descrição | Exemplo |
|---|---|---|
| `SL_URL` | URL do Service Layer (o script completa `https://` e `/b1s/v1` se faltar) | `https://servidor:50000/b1s/v1` |
| `SL_COMPANY_DB` | Base / schema da empresa | `SBO_TESTE_T10` |
| `SL_USER` | Usuário do **SAP B1** | `integracao` |
| `SL_PASSWORD` | Senha do usuário do SAP B1 | |
| `SL_VERIFY_SSL` | Validar certificado HTTPS | `false` |
| `SL_TIMEOUT` | Tempo limite das requisições, em segundos | `60` |
| `STATE_FILE` | Nome-base do arquivo de estado (o `CompanyDB` é acrescentado) | `sync_autores_state.json` |

### HANA direto (`main_hana.py`)

| Variável | Descrição | Exemplo |
|---|---|---|
| `HANA_HOST` | Servidor HANA (sem `http://`) | `hana-eqnx.datacore.com.br` |
| `HANA_PORT` | Porta SQL do HANA | `21226` |
| `HANA_USER` | Usuário do **banco** HANA | |
| `HANA_PASSWORD` | Senha do banco HANA | |
| `HANA_SCHEMA` | Schema da empresa | `SBO_TESTE_T10` |
| `HANA_ENCRYPT` | Conexão criptografada (TLS) | `true` |
| `HANA_VALIDATE_CERT` | Validar certificado TLS | `false` |
| `HANA_STATE_FILE` | Nome-base do arquivo de estado (o schema é acrescentado) | `sync_autores_state_hana.json` |

### Comum

| Variável | Descrição | Padrão |
|---|---|---|
| `DRY_RUN` | `true` apenas simula e registra no log; `false` grava no SAP B1 | `true` |
| `RULES_FILE` | Arquivo de regras departamento → modelos | `regras_autores.json` |
| `ONLY_ACTIVE_TEMPLATES` | `true` inclui só modelos ativos; `false` inclui também os inativos | `true` |

> O `.env` é carregado com prioridade sobre as variáveis de ambiente do sistema.
> Para mudar uma configuração (por exemplo, `DRY_RUN`), altere o próprio `.env`:
> definir a variável no terminal não tem efeito.

## Execução

```powershell
py .\main.py          # via Service Layer
py .\main_hana.py     # via HANA direto (testes)
```

### Fluxo recomendado

1. **Primeira execução em cada base:** o script apenas grava o ponto de partida (o
   maior `USERID` atual) e não altera nada. Usuários já existentes não são tocados.
2. Cadastre um usuário de teste no SAP B1.
3. Rode com `DRY_RUN=true` e confira no log:
   - o departamento do usuário e os prefixos aplicados;
   - os avisos de usuários cujo departamento não tem regra;
   - a conversão do nome para maiúsculas;
   - a lista de modelos em que ele seria incluído.
4. Se estiver correto, altere para `DRY_RUN=false` e rode novamente.
5. No SAP B1, reabra *Modelos de autorização – Definição* e confira a aba *Autor*.

> Em `DRY_RUN=true` o ponto de partida **não** avança. O mesmo usuário continua
> sendo tratado como novo até uma execução com `DRY_RUN=false`.

Para reprocessar usuários em testes, edite o arquivo de estado e reduza o valor
de `last_internal_key`.

## Agendamento (Windows)

Crie uma tarefa no **Agendador de Tarefas** para rodar a cada 5 a 15 minutos:

- **Programa:** `C:\caminho\automacao_cadastro_usuario\.venv\Scripts\python.exe`
- **Argumentos:** `main.py`
- **Iniciar em:** `C:\caminho\automacao_cadastro_usuario`

O `.env`, os logs e os arquivos de estado são lidos e gravados na pasta do
script, independentemente do diretório de execução.

## Solução de problemas

| Erro | Causa provável | Solução |
|---|---|---|
| `No connection adapters were found` | `SL_URL` sem `https://` | Corrigir a URL no `.env` |
| `Connection was reset` ao acessar o Service Layer | A porta informada não é do Service Layer (ex.: porta SQL do HANA) ou o IP está bloqueado | Confirmar a URL/porta com o provedor e liberar o IP |
| `invalid SERVERNODE '[http://...]'` | `HANA_HOST` com `http://` | Informar só o nome do servidor |
| `invalid schema name` | `HANA_SCHEMA` / `SL_COMPANY_DB` incorreto | Conferir o nome da base |
| Erro de SSL na conexão HANA | Servidor sem TLS na porta SQL | `HANA_ENCRYPT=false` |
| `Arquivo de regras não encontrado` | `regras_autores.json` ausente | Copiar `regras_autores.example.json` e ajustar os códigos |
| `sem regra: NÃO incluído em modelos` no log | Departamento do usuário não está em `regras_autores.json` | Incluir o departamento nas regras ou tratar o usuário manualmente |
| `Estrutura da WTM1 diferente do esperado` | Colunas obrigatórias extras na `WTM1` | Ajustar o `INSERT` no `main_hana.py` |
| Autor não aparece na tela do B1 | Cache do cliente B1 | Reabrir a tela ou fazer logoff/logon |

## Limitações conhecidas

- Mudanças de departamento **depois** do processamento não movem o usuário entre
  modelos.
- Usuários de departamentos **sem regra** não são reprocessados depois: o ponto de
  partida avança mesmo assim. Inclua-os manualmente ou, após criar a regra, reduza
  o `last_internal_key` no arquivo de estado e rode de novo.
- O script **não remove** autores (por exemplo, usuários bloqueados ou desligados).
- No `main.py`, as alterações são feitas em várias requisições, sem transação
  única. Em caso de falha, basta rodar de novo: o script é idempotente e o ponto
  de partida só avança quando tudo termina com sucesso.
- Se o usuário for autor em vários modelos do mesmo tipo de documento e as
  condições de mais de um forem atendidas, o SAP B1 gera aprovação em todos eles.
  Revise a aba *Condições* dos modelos.

## Segurança

- Nunca versione o `.env` (ele já está no `.gitignore`).
- Se o `.env` foi commitado por engano:
  ```powershell
  git rm --cached .env
  git commit -m "chore: remover .env do versionamento"
  ```
  Se ele chegou a um repositório remoto, troque as senhas.
- Restrinja o acesso à pasta do projeto ao usuário que executa a tarefa agendada.