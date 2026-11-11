"""
[AMBIENTE DE TESTES] Inclui NOVOS usuários do SAP B1 como AUTORES nos Modelos
de Autorização gravando DIRETO no banco SAP HANA (sem Service Layer).

ATENÇÃO: gravar em tabelas do SAP B1 via SQL NÃO é suportado pela SAP.
Use apenas em homologação para validar a lógica. Em produção, use main.py
(Service Layer).

Regras:
  - Cada departamento (OUDP.Code) recebe os modelos cujo nome começa com um
    dos prefixos definidos em regras_autores.json (ver regras_autores.example.json)
  - Usuário-espelho: em "espelhos" de regras_autores.json, um departamento pode
    apontar para um usuário de referência; o usuário recebe os mesmos modelos que
    o espelho tem hoje. O espelho tem prioridade sobre os prefixos.
  - Departamento sem regra (e "default": null) -> usuário NÃO é incluído (aviso no log)
  - CORRIGIR_USUARIOS (no .env): lista de USER_CODEs já existentes que também são
    tratados pela regra (só inclui modelos faltantes; nunca remove vínculos)
  - Nome do usuário (OUSR.U_NAME) gravado sempre em MAIÚSCULAS
  - Arquivo de estado separado por schema (evita reaproveitar o estado de outra base)
  - Ao final de toda execução, lista os usuários ativos (não bloqueados) que não
    estão em nenhum modelo de autorização (log + usuarios_sem_modelo_<SCHEMA>.csv).
    Apenas relatório: nada é gravado no banco.

Tabelas:
  OUSR (usuários)  OUDP (departamentos)  OWTM (modelos)  WTM1 (autores)
"""
import csv
import json
import os
import sys
import logging
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from hdbcli import dbapi

# --------------------------------------------------------------------------
# Configuração (.env na mesma pasta do script)
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)  # .env prevalece sobre variáveis do sistema

HANA_HOST = (os.environ["HANA_HOST"].strip()
             .removeprefix("https://").removeprefix("http://").rstrip("/"))
HANA_PORT = int(os.environ["HANA_PORT"])
HANA_USER = os.environ["HANA_USER"]
HANA_PASSWORD = os.environ["HANA_PASSWORD"]
HANA_SCHEMA = os.environ["HANA_SCHEMA"]
HANA_ENCRYPT = os.getenv("HANA_ENCRYPT", "true").lower() == "true"
HANA_VALIDATE_CERT = os.getenv("HANA_VALIDATE_CERT", "false").lower() == "true"

# Padrão do script: DRY_RUN = true (apenas loga, não grava nada). Para gravar, use DRY_RUN=false
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

_state_base = Path(os.getenv("HANA_STATE_FILE", "sync_autores_state_hana.json"))

# O schema entra SEMPRE no nome: cada base tem seu próprio ponto de partida
STATE_FILE = BASE_DIR / f"{_state_base.stem}_{HANA_SCHEMA}{_state_base.suffix}"
RULES_FILE = BASE_DIR / os.getenv("RULES_FILE", "regras_autores.json")
UNLINKED_FILE = BASE_DIR / f"usuarios_sem_modelo_{HANA_SCHEMA}.csv"

# USER_CODEs de usuários JÁ existentes a corrigir pela regra do departamento,
# separados por vírgula (ex.: CORRIGIR_USUARIOS=grazielle.correa,joao.silva).
# Só inclui modelos faltantes; nunca remove. Respeita DRY_RUN.
FIX_USERS = os.getenv("CORRIGIR_USUARIOS", "")
ONLY_ACTIVE_TEMPLATES = os.getenv("ONLY_ACTIVE_TEMPLATES", "true").lower() == "true"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(BASE_DIR / "sync_autores_hana.log", encoding="utf-8"),
              logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

S = f'"{HANA_SCHEMA}"'   # schema entre aspas (HANA é case-sensitive)


# --------------------------------------------------------------------------
# Banco
# --------------------------------------------------------------------------
def connect() -> dbapi.Connection:
    conn = dbapi.connect(
        address=HANA_HOST,
        port=HANA_PORT,
        user=HANA_USER,
        password=HANA_PASSWORD,
        encrypt=HANA_ENCRYPT,
        sslValidateCertificate=HANA_VALIDATE_CERT,
    )
    conn.setautocommit(False)
    return conn


def fetch(conn, sql: str, params: tuple = ()) -> list[tuple]:
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        return [tuple(r) for r in cur.fetchall()]  # ResultRow -> tuple
    finally:
        cur.close()


def check_wtm1_structure(conn) -> None:
    """Garante que WTM1 só exige WtmCode e UserID; caso contrário, aborta."""
    rows = fetch(conn, """
        SELECT "COLUMN_NAME", "IS_NULLABLE", "DEFAULT_VALUE"
          FROM SYS.TABLE_COLUMNS
         WHERE "SCHEMA_NAME" = ? AND "TABLE_NAME" = 'WTM1'
         ORDER BY "POSITION"
    """, (HANA_SCHEMA,))
    if not rows:
        raise RuntimeError(f"Tabela WTM1 não encontrada no schema {HANA_SCHEMA}.")
    cols = [r[0] for r in rows]
    log.info("Colunas da WTM1: %s", ", ".join(cols))
    required_extra = [r[0] for r in rows
                      if r[0] not in ("WtmCode", "UserID")
                      and r[1] == "FALSE" and r[2] is None]
    if "WtmCode" not in cols or "UserID" not in cols or required_extra:
        raise RuntimeError(
            "Estrutura da WTM1 diferente do esperado. Colunas obrigatórias extras: "
            f"{required_extra}. Envie essa lista para ajustar o script.")


# --------------------------------------------------------------------------
# Estado (último USERID processado)
# --------------------------------------------------------------------------
def load_last_key() -> int | None:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8")).get("last_internal_key")
    return None


def save_last_key(key: int) -> None:
    STATE_FILE.write_text(json.dumps({"last_internal_key": key}), encoding="utf-8")


# --------------------------------------------------------------------------
# Regras
# --------------------------------------------------------------------------
def load_rules() -> dict:
    """Lê regras_autores.json:
    {"default": [...]|null,
     "departments": {"<Code>": [prefixos]},
     "espelhos":    {"<Code>": "<USER_CODE do usuário-espelho>"}}
    O espelho tem prioridade sobre os prefixos do mesmo departamento."""
    if not RULES_FILE.exists():
        raise RuntimeError(f"Arquivo de regras não encontrado: {RULES_FILE}. "
                           "Copie regras_autores.example.json e ajuste os códigos.")
    rules = json.loads(RULES_FILE.read_text(encoding="utf-8"))
    rules["departments"] = {int(k): v for k, v in rules.get("departments", {}).items()}
    rules["espelhos"] = {int(k): v.strip().lower()
                         for k, v in rules.get("espelhos", {}).items() if v and v.strip()}
    return rules


def prefixes_for(dept_code, rules: dict) -> list[str] | None:
    return rules["departments"].get(dept_code, rules.get("default"))


def template_matches(name: str, prefixes: list[str]) -> bool:
    n = (name or "").strip().upper()
    return any(n.startswith(p.upper()) for p in prefixes)


def expected_templates(dept, rules: dict, templates: dict, links: dict,
                       by_code: dict) -> tuple[set | None, str]:
    """Modelos que um usuário do departamento deve ter e a origem da regra.
    Retorna (None, motivo) quando não há regra aplicável."""
    mirror_code = rules["espelhos"].get(dept)
    if mirror_code:
        mirror = by_code.get(mirror_code)
        if mirror is None:
            return None, f"espelho '{mirror_code}' não encontrado na OUSR"
        if mirror[4] == "Y":
            return None, f"espelho '{mirror[1]}' está bloqueado"
        target = links[mirror[0]] & templates.keys()
        if not target:
            return None, f"espelho '{mirror[1]}' não está em nenhum modelo"
        return target, f"espelho: {mirror[1]} ({len(target)} modelos)"
    prefixes = prefixes_for(dept, rules)
    if prefixes:
        target = {w for w, n in templates.items() if template_matches(n, prefixes)}
        return target, f"prefixos: {', '.join(prefixes)} ({len(target)} modelos)"
    return None, "sem regra"


# --------------------------------------------------------------------------
# Relatório
# --------------------------------------------------------------------------
def report_unlinked(conn, dept_names: dict) -> list[tuple]:
    """Lista usuários ativos sem nenhuma linha na WTM1 (log + CSV). Não grava no banco."""
    rows = fetch(conn, f"""
        SELECT u."USERID", u."USER_CODE", u."U_NAME", u."Department"
          FROM {S}."OUSR" u
         WHERE IFNULL(u."Locked", 'N') <> 'Y'
           AND NOT EXISTS (SELECT 1 FROM {S}."WTM1" w WHERE w."UserID" = u."USERID")
         ORDER BY u."USERID"
    """)

    with UNLINKED_FILE.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["USERID", "USER_CODE", "U_NAME", "Department", "DeptName"])
        for uid, code, name, dept in rows:
            writer.writerow([uid, code, name, dept, dept_names.get(dept, "")])

    if not rows:
        log.info("Todos os usuários ativos estão em pelo menos um modelo de autorização.")
    else:
        log.warning("%d usuário(s) ativo(s) sem nenhum modelo de autorização (ver %s):",
                    len(rows), UNLINKED_FILE.name)
        for uid, code, name, dept in rows:
            log.warning("  %s - %s | depto %s (%s)", code, name, dept,
                        dept_names.get(dept, "(sem departamento)"))
    return rows


# --------------------------------------------------------------------------
# Sincronização
# --------------------------------------------------------------------------
def sync() -> None:
    conn = connect()
    log.info("Conectado ao HANA %s:%s, schema %s | estado: %s | regras: %s",
             HANA_HOST, HANA_PORT, HANA_SCHEMA, STATE_FILE.name, RULES_FILE.name)
    try:
        dept_names = dict(fetch(conn, f'SELECT "Code", "Name" FROM {S}."OUDP"'))
        _sync_new_users(conn, dept_names)
        # Depois do commit: os usuários recém-incluídos já não aparecem na lista
        report_unlinked(conn, dept_names)
    finally:
        conn.close()


def _sync_new_users(conn, dept_names: dict) -> None:
    users = fetch(conn, f"""
        SELECT "USERID", "USER_CODE", "U_NAME", "Department", "Locked"
          FROM {S}."OUSR"
    """)
    max_key = max(u[0] for u in users)
    by_code = {(u[1] or "").strip().lower(): u for u in users}

    last_key = load_last_key()
    if last_key is None:
        save_last_key(max_key)
        log.info("Primeira execução: ponto de partida gravado (USERID=%s). "
                 "Nenhum usuário novo tratado.", max_key)
        new_users = []
    else:
        new_users = [u for u in users if u[0] > last_key and u[4] != "Y"]
        if not new_users:
            log.info("Nenhum usuário novo desde USERID=%s.", last_key)

    # Usuários JÁ existentes indicados para correção (CORRIGIR_USUARIOS no .env)
    new_ids = {u[0] for u in new_users}
    fix_users = []
    for c in sorted({c.strip().lower() for c in FIX_USERS.split(",") if c.strip()}):
        u = by_code.get(c)
        if u is None:
            log.warning("CORRIGIR_USUARIOS: usuário '%s' não encontrado na OUSR.", c)
        elif u[4] == "Y":
            log.warning("CORRIGIR_USUARIOS: usuário '%s' está bloqueado; ignorado.", u[1])
        elif u[0] not in new_ids:
            fix_users.append(u)

    if not new_users and not fix_users:
        return

    rules = load_rules()

    tpl_sql = f'SELECT "WtmCode", "Name", "Active" FROM {S}."OWTM"'
    if ONLY_ACTIVE_TEMPLATES:
        tpl_sql += ' WHERE "Active" = \'Y\''
    templates = {w: n for w, n, _ in fetch(conn, tpl_sql)}

    links = defaultdict(set)   # USERID -> modelos em que já é autor
    for w, u in fetch(conn, f'SELECT "WtmCode", "UserID" FROM {S}."WTM1"'):
        links[u].add(w)

    to_rename, to_insert, skipped = [], [], []
    for kind, (uid, code, name, dept, _) in ([("Novo", u) for u in new_users]
                                             + [("Correção", u) for u in fix_users]):
        upper_name = (name or "").upper()
        if name and name != upper_name:
            to_rename.append((upper_name, uid))
            log.info("Usuário %s: nome '%s' -> '%s'", code, name, upper_name)
        depto = dept_names.get(dept, "(sem departamento)")
        target, origem = expected_templates(dept, rules, templates, links, by_code)
        if target is None:
            skipped.append(code)
            log.warning("%s usuário %s | depto %s (%s) | %s: NÃO incluído em modelos.",
                        kind, code, dept, depto, origem)
            continue
        missing = sorted(target - links[uid])
        extra = len((links[uid] & templates.keys()) - target)
        log.info("%s usuário %s - %s | depto %s (%s) | %s | já tem %d, faltam %d%s",
                 kind, code, upper_name, dept, depto, origem,
                 len(target & links[uid]), len(missing),
                 f", {extra} fora da regra (mantidos)" if extra else "")
        for w in missing:
            log.info("    + %s %s", w, templates[w])
        to_insert += [(w, uid) for w in missing]

    if skipped:
        log.warning("%d usuário(s) sem regra de departamento (tratar manualmente): %s",
                    len(skipped), ", ".join(skipped))

    if not to_insert and not to_rename:
        log.info("Nada a alterar.")
    elif DRY_RUN:
        log.info("DRY_RUN ativo: %d nomes seriam convertidos para maiúsculas "
                 "e %d linhas seriam inseridas na WTM1. Nada gravado.",
                 len(to_rename), len(to_insert))
        return
    else:
        if to_insert:
            check_wtm1_structure(conn)
        cur = conn.cursor()
        try:
            # Tudo na mesma transação: ou grava tudo, ou nada
            if to_rename:
                cur.executemany(
                    f'UPDATE {S}."OUSR" SET "U_NAME" = ? WHERE "USERID" = ?',
                    to_rename)
            if to_insert:
                cur.executemany(
                    f'INSERT INTO {S}."WTM1" ("WtmCode", "UserID") VALUES (?, ?)',
                    to_insert)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
        log.info("%d nomes convertidos para maiúsculas; %d linhas inseridas na WTM1.",
                 len(to_rename), len(to_insert))

    if new_users:
        save_last_key(max(u[0] for u in new_users))
        log.info("Concluído. Ponto de partida atualizado.")
    else:
        log.info("Concluído.")


if __name__ == "__main__":
    try:
        sync()
    except Exception:
        log.exception("Falha na sincronização")
        sys.exit(1)
