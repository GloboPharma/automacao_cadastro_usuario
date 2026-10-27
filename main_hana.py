"""
[AMBIENTE DE TESTES] Inclui NOVOS usuários do SAP B1 como AUTORES nos Modelos
de Autorização gravando DIRETO no banco SAP HANA (sem Service Layer).

ATENÇÃO: gravar em tabelas do SAP B1 via SQL NÃO é suportado pela SAP.
Use apenas em homologação para validar a lógica. Em produção, use main.py
(Service Layer).

Regra:
  - Usuário de VENDAS      -> todos os modelos cujo nome começa com "PV"
  - Usuário NÃO de vendas  -> todos os modelos que NÃO começam com "PV"

Tabelas:
  OUSR (usuários)  OUDP (departamentos)  OWTM (modelos)  WTM1 (autores)
"""
import json
import os
import sys
import logging
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
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
STATE_FILE = BASE_DIR / os.getenv("HANA_STATE_FILE", "sync_autores_state_hana.json")

SALES_TEMPLATE_PREFIX = "PV"
SALES_DEPARTMENT_CODES: set[int] = set()          # ex.: {3, 8}
SALES_DEPARTMENT_KEYWORDS = ["VENDA", "COMERCIAL"]
ONLY_ACTIVE_TEMPLATES = False

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
def is_sales_template(name: str) -> bool:
    return (name or "").strip().upper().startswith(SALES_TEMPLATE_PREFIX.upper())


def is_sales_user(dept_code, dept_names: dict) -> bool:
    if dept_code in SALES_DEPARTMENT_CODES:
        return True
    name = (dept_names.get(dept_code) or "").upper()
    return any(k.upper() in name for k in SALES_DEPARTMENT_KEYWORDS)


# --------------------------------------------------------------------------
# Sincronização
# --------------------------------------------------------------------------
def sync() -> None:
    conn = connect()
    log.info("Conectado ao HANA %s:%s, schema %s", HANA_HOST, HANA_PORT, HANA_SCHEMA)
    try:
        users = fetch(conn, f"""
            SELECT "USERID", "USER_CODE", "U_NAME", "Department", "Locked"
              FROM {S}."OUSR"
        """)
        max_key = max(u[0] for u in users)

        last_key = load_last_key()
        if last_key is None:
            save_last_key(max_key)
            log.info("Primeira execução: ponto de partida gravado (USERID=%s). "
                     "Nenhuma alteração feita.", max_key)
            return

        new_users = [u for u in users if u[0] > last_key and u[4] != "Y"]
        if not new_users:
            log.info("Nenhum usuário novo desde USERID=%s.", last_key)
            return

        dept_names = dict(fetch(conn, f'SELECT "Code", "Name" FROM {S}."OUDP"'))

        sales_users, other_users, to_rename = [], [], []
        for uid, code, name, dept, _ in new_users:
            sales = is_sales_user(dept, dept_names)
            (sales_users if sales else other_users).append((uid, code))
            upper_name = (name or "").upper()
            if name and name != upper_name:
                to_rename.append((upper_name, uid))
                log.info("Usuário %s: nome '%s' -> '%s'", code, name, upper_name)
            log.info("Novo usuário %s - %s | depto: %s | %s", code, upper_name,
                     dept_names.get(dept, "(sem departamento)"),
                     "VENDAS" if sales else "NÃO VENDAS")

        tpl_sql = f'SELECT "WtmCode", "Name", "Active" FROM {S}."OWTM"'
        if ONLY_ACTIVE_TEMPLATES:
            tpl_sql += ' WHERE "Active" = \'Y\''
        templates = fetch(conn, tpl_sql)

        existing = set(fetch(conn, f'SELECT "WtmCode", "UserID" FROM {S}."WTM1"'))

        to_insert = []
        for wtm_code, tpl_name, _ in templates:
            targets = sales_users if is_sales_template(tpl_name) else other_users
            missing = [(uid, code) for uid, code in targets
                       if (wtm_code, uid) not in existing]
            if missing:
                log.info("Modelo %s (%s): +%s", wtm_code, tpl_name,
                         ", ".join(c for _, c in missing))
                to_insert += [(wtm_code, uid) for uid, _ in missing]

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

        save_last_key(max(u[0] for u in new_users))
        log.info("Concluído. Ponto de partida atualizado.")
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        sync()
    except Exception:
        log.exception("Falha na sincronização")
        sys.exit(1)