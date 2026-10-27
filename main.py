"""
Inclui NOVOS usuários do SAP Business One como AUTORES nos Modelos de Autorização, via SERVICE LAYER (forma suportada pela SAP).

Regras:
  - Usuário de VENDAS      -> todos os modelos cujo nome começa com "PV"
  - Usuário NÃO de vendas  -> todos os modelos que NÃO começam com "PV"
  - Nome do usuário (UserName / OUSR.U_NAME) gravado sempre em MAIÚSCULAS

"Novo usuário" = InternalKey maior que o último processado (arquivo de estado).
Na 1ª execução o script apenas grava o ponto de partida (não altera nada).

ANTES DE USAR EM PRODUÇÃO:
  1. Confira no $metadata (GET /b1s/v1/$metadata) os nomes: ApprovalTemplates -> ApprovalTemplateUsers -> UserID
  2. Rode com DRY_RUN=true e valide o log.
"""
import json
import os
import sys
import logging
from pathlib import Path

import requests
import urllib3
from dotenv import load_dotenv

# --------------------------------------------------------------------------
# Configuração (.env na mesma pasta do script)
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)


def normalize_sl_url(url: str) -> str:
    """Aceita 'host:porta', 'https://host:porta' ou a URL completa."""
    url = url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if not url.endswith("/b1s/v1") and not url.endswith("/b1s/v2"):
        url += "/b1s/v1"
    return url


SL_URL = normalize_sl_url(os.environ["SL_URL"])
COMPANY_DB = os.environ["SL_COMPANY_DB"]
SL_USER = os.environ["SL_USER"]
SL_PASSWORD = os.environ["SL_PASSWORD"]
VERIFY_SSL = os.getenv("SL_VERIFY_SSL", "false").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
STATE_FILE = BASE_DIR / os.getenv("STATE_FILE", "sync_autores_state.json")
TIMEOUT = int(os.getenv("SL_TIMEOUT", "60"))

SALES_TEMPLATE_PREFIX = "PV"
SALES_DEPARTMENT_CODES: set[int] = set()          # ex.: {3, 8}
SALES_DEPARTMENT_KEYWORDS = ["VENDA", "COMERCIAL"]
ONLY_ACTIVE_TEMPLATES = False

if not VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(BASE_DIR / "sync_autores.log", encoding="utf-8"),
              logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Cliente Service Layer
# --------------------------------------------------------------------------
class ServiceLayerError(RuntimeError):
    pass


def sl_error(r: requests.Response) -> str:
    """Extrai a mensagem de erro do Service Layer."""
    try:
        err = r.json().get("error", {})
        msg = err.get("message", {})
        msg = msg.get("value") if isinstance(msg, dict) else msg
        return f"HTTP {r.status_code} - código {err.get('code')}: {msg}"
    except ValueError:
        return f"HTTP {r.status_code}: {r.text[:500]}"


class ServiceLayer:
    def __init__(self) -> None:
        self.s = requests.Session()
        self.s.verify = VERIFY_SSL

    def _req(self, method: str, path: str, **kw) -> requests.Response:
        url = path if path.startswith("http") else f"{SL_URL}/{path}"
        r = self.s.request(method, url, timeout=TIMEOUT, **kw)
        if r.status_code >= 400:
            raise ServiceLayerError(f"{method} {path} falhou: {sl_error(r)}")
        return r

    def login(self) -> None:
        self._req("POST", "Login", json={
            "CompanyDB": COMPANY_DB, "UserName": SL_USER, "Password": SL_PASSWORD})
        log.info("Login no Service Layer OK (%s, base %s)", SL_URL, COMPANY_DB)

    def logout(self) -> None:
        try:
            self.s.post(f"{SL_URL}/Logout", timeout=TIMEOUT)
        except requests.RequestException:
            pass

    def get_all(self, path: str) -> list[dict]:
        items, nxt = [], path
        headers = {"Prefer": "odata.maxpagesize=500"}
        while nxt:
            data = self._req("GET", nxt, headers=headers).json()
            items.extend(data.get("value", []))
            nxt = data.get("odata.nextLink") or data.get("@odata.nextLink")
        return items

    def get(self, path: str) -> dict:
        return self._req("GET", path).json()

    def patch(self, path: str, body: dict, replace_collections: bool = False) -> None:
        headers = {"B1S-ReplaceCollectionsOnPatch": "true"} if replace_collections else {}
        self._req("PATCH", path, json=body, headers=headers)


# --------------------------------------------------------------------------
# Estado (último InternalKey processado)
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
    sl = ServiceLayer()
    sl.login()
    try:
        users = sl.get_all(
            "Users?$select=InternalKey,UserCode,UserName,Department,Locked")
        max_key = max(u["InternalKey"] for u in users)

        last_key = load_last_key()
        if last_key is None:
            save_last_key(max_key)
            log.info("Primeira execução: ponto de partida gravado (InternalKey=%s). "
                     "Nenhuma alteração feita.", max_key)
            return

        new_users = [u for u in users
                     if u["InternalKey"] > last_key and u.get("Locked") != "tYES"]
        if not new_users:
            log.info("Nenhum usuário novo desde InternalKey=%s.", last_key)
            return

        dept_names = {d["Code"]: d.get("Name", "")
                      for d in sl.get_all("Departments?$select=Code,Name")}

        sales_users, other_users, to_rename = [], [], []
        for u in new_users:
            sales = is_sales_user(u.get("Department"), dept_names)
            (sales_users if sales else other_users).append(u)
            name = u.get("UserName") or ""
            if name and name != name.upper():
                to_rename.append(u)
                log.info("Usuário %s: nome '%s' -> '%s'",
                         u["UserCode"], name, name.upper())
            log.info("Novo usuário %s - %s | depto: %s | %s",
                     u["UserCode"], name.upper(),
                     dept_names.get(u.get("Department"), "(sem departamento)"),
                     "VENDAS" if sales else "NÃO VENDAS")

        templates = sl.get_all("ApprovalTemplates?$select=Code,Name,IsActive")
        if ONLY_ACTIVE_TEMPLATES:
            templates = [t for t in templates if t.get("IsActive") == "tYES"]

        # Monta o plano: modelo -> (lista atual de autores, usuários a incluir)
        plan = []
        for t in templates:
            targets = sales_users if is_sales_template(t["Name"]) else other_users
            if not targets:
                continue
            tpl = sl.get(f"ApprovalTemplates({t['Code']})")
            current = tpl.get("ApprovalTemplateUsers", []) or []
            current_ids = {line["UserID"] for line in current}
            missing = [u for u in targets if u["InternalKey"] not in current_ids]
            if missing:
                log.info("Modelo %s (%s): +%s", t["Code"], t["Name"],
                         ", ".join(u["UserCode"] for u in missing))
                plan.append((t, current, missing))

        if not plan and not to_rename:
            log.info("Nada a alterar.")
        elif DRY_RUN:
            log.info("DRY_RUN ativo: %d nomes seriam convertidos para maiúsculas e "
                     "%d inclusões em %d modelos. Nada gravado.",
                     len(to_rename), sum(len(m) for _, _, m in plan), len(plan))
            return
        else:
            # 1) Nome em maiúsculas
            for u in to_rename:
                sl.patch(f"Users({u['InternalKey']})",
                         {"UserName": u["UserName"].upper()})
            # 2) Autores nos modelos (lista completa substitui a coleção)
            for t, current, missing in plan:
                new_list = [{"UserID": line["UserID"]} for line in current]
                new_list += [{"UserID": u["InternalKey"]} for u in missing]
                sl.patch(f"ApprovalTemplates({t['Code']})",
                         {"ApprovalTemplateUsers": new_list},
                         replace_collections=True)
            log.info("%d nomes convertidos; %d inclusões em %d modelos.",
                     len(to_rename), sum(len(m) for _, _, m in plan), len(plan))

        save_last_key(max(u["InternalKey"] for u in new_users))
        log.info("Concluído. Ponto de partida atualizado.")
    finally:
        sl.logout()


if __name__ == "__main__":
    try:
        sync()
    except Exception:
        log.exception("Falha na sincronização")
        sys.exit(1)