"""
Inclui NOVOS usuários do SAP Business One como AUTORES nos Modelos de Autorização, via SERVICE LAYER (forma suportada pela SAP).

Regras:
  - Cada departamento (Departments.Code) recebe os modelos cujo nome começa com
    um dos prefixos definidos em regras_autores.json (ver regras_autores.example.json)
  - Usuário-espelho: em "espelhos" de regras_autores.json, um departamento pode
    apontar para um usuário de referência; o usuário recebe os mesmos modelos que
    o espelho tem hoje. O espelho tem prioridade sobre os prefixos.
  - Departamento sem regra (e "default": null) -> usuário NÃO é incluído (aviso no log)
  - CORRIGIR_USUARIOS (no .env): lista de UserCodes já existentes que também são
    tratados pela regra (só inclui modelos faltantes; nunca remove vínculos)
  - Nome do usuário (UserName / OUSR.U_NAME) gravado sempre em MAIÚSCULAS
  - Ao final de toda execução, lista os usuários ativos (não bloqueados) que não
    estão em nenhum modelo de autorização (log + usuarios_sem_modelo_<CompanyDB>.csv).
    Apenas relatório: nada é gravado no SAP.

"Novo usuário" = InternalKey maior que o último processado (arquivo de estado,
separado por base: o nome do CompanyDB entra no nome do arquivo).
Na 1ª execução em cada base o script apenas grava o ponto de partida (não trata
usuários novos; CORRIGIR_USUARIOS e o relatório funcionam normalmente).

ANTES DE USAR EM PRODUÇÃO:
  1. Confira no $metadata (GET /b1s/v1/$metadata) os nomes: ApprovalTemplates -> ApprovalTemplateUsers -> UserID
  2. Rode com DRY_RUN=true e valide o log.
"""
import csv
import json
import os
import sys
import logging
from collections import defaultdict
from pathlib import Path

import requests
import urllib3
from dotenv import load_dotenv

# --------------------------------------------------------------------------
# Configuração (.env na mesma pasta do script)
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)  # .env prevalece sobre variáveis do sistema


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

# Padrão do script: DRY_RUN = true (apenas loga, não grava nada). Para gravar, use DRY_RUN=false
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

_state_base = Path(os.getenv("STATE_FILE", "sync_autores_state.json"))
# O CompanyDB entra SEMPRE no nome: cada base tem seu próprio ponto de partida
STATE_FILE = BASE_DIR / f"{_state_base.stem}_{COMPANY_DB}{_state_base.suffix}"
RULES_FILE = BASE_DIR / os.getenv("RULES_FILE", "regras_autores.json")
UNLINKED_FILE = BASE_DIR / f"usuarios_sem_modelo_{COMPANY_DB}.csv"
TIMEOUT = int(os.getenv("SL_TIMEOUT", "60"))

# UserCodes de usuários JÁ existentes a corrigir pela regra do departamento,
# separados por vírgula (ex.: CORRIGIR_USUARIOS=grazielle.correa,joao.silva).
# Só inclui modelos faltantes; nunca remove. Respeita DRY_RUN.
FIX_USERS = os.getenv("CORRIGIR_USUARIOS", "")
ONLY_ACTIVE_TEMPLATES = os.getenv("ONLY_ACTIVE_TEMPLATES", "true").lower() == "true"

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
        log.info("Login no Service Layer OK (%s, base %s) | estado: %s | regras: %s",
                 SL_URL, COMPANY_DB, STATE_FILE.name, RULES_FILE.name)

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


def load_templates(sl: ServiceLayer) -> dict:
    """Todos os modelos (ativos e inativos) com seus autores:
    {Code: {"name", "active", "users": [linhas ApprovalTemplateUsers]}}."""
    rows = sl.get_all("ApprovalTemplates")
    result = {}
    for t in rows:
        users = t.get("ApprovalTemplateUsers")
        if users is None:   # versão do SL que não devolve a coleção na listagem
            users = sl.get(f"ApprovalTemplates({t['Code']})").get("ApprovalTemplateUsers")
        result[t["Code"]] = {"name": t.get("Name") or "",
                             "active": t.get("IsActive") == "tYES",
                             "users": users or []}
    return result


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
def load_rules() -> dict:
    """Lê regras_autores.json:
    {"default": [...]|null,
     "departments": {"<Code>": [prefixos]},
     "espelhos":    {"<Code>": "<UserCode do usuário-espelho>"}}
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
            return None, f"espelho '{mirror_code}' não encontrado em Users"
        if mirror.get("Locked") == "tYES":
            return None, f"espelho '{mirror['UserCode']}' está bloqueado"
        target = links[mirror["InternalKey"]] & templates.keys()
        if not target:
            return None, f"espelho '{mirror['UserCode']}' não está em nenhum modelo"
        return target, f"espelho: {mirror['UserCode']} ({len(target)} modelos)"
    prefixes = prefixes_for(dept, rules)
    if prefixes:
        target = {c for c, n in templates.items() if template_matches(n, prefixes)}
        return target, f"prefixos: {', '.join(prefixes)} ({len(target)} modelos)"
    return None, "sem regra"


# --------------------------------------------------------------------------
# Relatório
# --------------------------------------------------------------------------
def report_unlinked(users: list[dict], links: dict, dept_names: dict) -> list[dict]:
    """Lista usuários ativos que não são autores em nenhum modelo (log + CSV).
    Não grava no SAP."""
    rows = sorted((u for u in users
                   if u.get("Locked") != "tYES" and not links[u["InternalKey"]]),
                  key=lambda u: u["InternalKey"])

    with UNLINKED_FILE.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["InternalKey", "UserCode", "UserName", "Department", "DeptName"])
        for u in rows:
            dept = u.get("Department")
            writer.writerow([u["InternalKey"], u["UserCode"], u.get("UserName") or "",
                             dept, dept_names.get(dept, "")])

    if not rows:
        log.info("Todos os usuários ativos estão em pelo menos um modelo de autorização.")
    else:
        log.warning("%d usuário(s) ativo(s) sem nenhum modelo de autorização (ver %s):",
                    len(rows), UNLINKED_FILE.name)
        for u in rows:
            dept = u.get("Department")
            log.warning("  %s - %s | depto %s (%s)", u["UserCode"], u.get("UserName") or "",
                        dept, dept_names.get(dept, "(sem departamento)"))
    return rows


# --------------------------------------------------------------------------
# Sincronização
# --------------------------------------------------------------------------
def sync() -> None:
    sl = ServiceLayer()
    sl.login()
    try:
        dept_names = {d["Code"]: d.get("Name", "")
                      for d in sl.get_all("Departments?$select=Code,Name")}
        users = sl.get_all(
            "Users?$select=InternalKey,UserCode,UserName,Department,Locked")
        all_templates = load_templates(sl)

        links = defaultdict(set)   # InternalKey -> modelos em que já é autor
        for code, t in all_templates.items():
            for line in t["users"]:
                links[line["UserID"]].add(code)

        _sync_new_users(sl, users, all_templates, links, dept_names)
        # links já reflete o que foi gravado: os recém-incluídos não aparecem na lista
        report_unlinked(users, links, dept_names)
    finally:
        sl.logout()


def _sync_new_users(sl: ServiceLayer, users: list[dict], all_templates: dict,
                    links: dict, dept_names: dict) -> None:
    max_key = max(u["InternalKey"] for u in users)
    by_code = {(u.get("UserCode") or "").strip().lower(): u for u in users}

    last_key = load_last_key()
    if last_key is None:
        save_last_key(max_key)
        log.info("Primeira execução: ponto de partida gravado (InternalKey=%s). "
                 "Nenhum usuário novo tratado.", max_key)
        new_users = []
    else:
        new_users = [u for u in users
                     if u["InternalKey"] > last_key and u.get("Locked") != "tYES"]
        if not new_users:
            log.info("Nenhum usuário novo desde InternalKey=%s.", last_key)

    # Usuários JÁ existentes indicados para correção (CORRIGIR_USUARIOS no .env)
    new_ids = {u["InternalKey"] for u in new_users}
    fix_users = []
    for c in sorted({c.strip().lower() for c in FIX_USERS.split(",") if c.strip()}):
        u = by_code.get(c)
        if u is None:
            log.warning("CORRIGIR_USUARIOS: usuário '%s' não encontrado em Users.", c)
        elif u.get("Locked") == "tYES":
            log.warning("CORRIGIR_USUARIOS: usuário '%s' está bloqueado; ignorado.",
                        u["UserCode"])
        elif u["InternalKey"] not in new_ids:
            fix_users.append(u)

    if not new_users and not fix_users:
        return

    rules = load_rules()

    # Modelos elegíveis para inclusão: Code -> Name
    templates = {c: t["name"] for c, t in all_templates.items()
                 if t["active"] or not ONLY_ACTIVE_TEMPLATES}

    to_rename, skipped = [], []
    to_insert = defaultdict(list)   # Code do modelo -> InternalKeys a incluir
    for kind, u in ([("Novo", u) for u in new_users]
                    + [("Correção", u) for u in fix_users]):
        uid, code = u["InternalKey"], u["UserCode"]
        name = u.get("UserName") or ""
        upper_name = name.upper()
        if name and name != upper_name:
            to_rename.append(u)
            log.info("Usuário %s: nome '%s' -> '%s'", code, name, upper_name)
        dept = u.get("Department")
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
        for c in missing:
            log.info("    + %s %s", c, templates[c])
            to_insert[c].append(uid)

    if skipped:
        log.warning("%d usuário(s) sem regra de departamento (tratar manualmente): %s",
                    len(skipped), ", ".join(skipped))

    n_insert = sum(len(v) for v in to_insert.values())
    if not to_insert and not to_rename:
        log.info("Nada a alterar.")
    elif DRY_RUN:
        log.info("DRY_RUN ativo: %d nomes seriam convertidos para maiúsculas e "
                 "%d inclusões em %d modelos. Nada gravado.",
                 len(to_rename), n_insert, len(to_insert))
        return
    else:
        # 1) Nome em maiúsculas
        for u in to_rename:
            sl.patch(f"Users({u['InternalKey']})", {"UserName": u["UserName"].upper()})
        # 2) Autores nos modelos (lista completa substitui a coleção).
        #    Relê o modelo logo antes de gravar para não perder autores incluídos
        #    por outra pessoa desde a leitura inicial.
        for c, uids in sorted(to_insert.items()):
            current = sl.get(f"ApprovalTemplates({c})").get("ApprovalTemplateUsers") or []
            current_ids = {line["UserID"] for line in current}
            add = [uid for uid in uids if uid not in current_ids]
            if add:
                new_list = [{"UserID": line["UserID"]} for line in current]
                new_list += [{"UserID": uid} for uid in add]
                sl.patch(f"ApprovalTemplates({c})", {"ApprovalTemplateUsers": new_list},
                         replace_collections=True)
            for uid in uids:
                links[uid].add(c)
        log.info("%d nomes convertidos para maiúsculas; %d inclusões em %d modelos.",
                 len(to_rename), n_insert, len(to_insert))

    if new_users:
        save_last_key(max(u["InternalKey"] for u in new_users))
        log.info("Concluído. Ponto de partida atualizado.")
    else:
        log.info("Concluído.")


if __name__ == "__main__":
    try:
        sync()
    except Exception:
        log.exception("Falha na sincronização")
        sys.exit(1)
