"""人物对照：把各系统里的账号名对到同一个人。表里没有的账号自动登记为「未核对」。"""
import hashlib
import json
import re

from .util import ROOT

_FILE = ROOT / "config" / "people.json"
_ROLES_FILE = ROOT / "config" / "campaign_roles.json"


def _account_key(value):
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def name_key(name):
    """「alejandra.segovia」和「Alejandra Segovia」算同一个名字。"""
    return re.sub(r"[\s._-]+", " ", name or "").strip().casefold()


def looks_like_name(account):
    """「alejandra.segovia」这类账号写成「Alejandra Segovia」；其他账号原样。"""
    if re.fullmatch(r"[a-z]+(?:[._][a-z]+)+", account or ""):
        return " ".join(w.capitalize() for w in re.split(r"[._]", account))
    return account


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.casefold()).strip("-") or "x"


class Roster:
    def __init__(self):
        self.people = {}      # id -> dict
        self.index = {}       # (system, 账号.casefold()) -> id
        self.used = []        # 按首次出现的顺序
        self.dynamic = []     # 自动登记的人（未核对）
        for p in json.loads(_FILE.read_text(encoding="utf-8"))["people"]:
            self.people[p["id"]] = dict(p)
            for system in ("slack", "femini", "github", "hubspot", "email"):
                for acct in p.get(system, []):
                    key = (system, _account_key(acct))
                    previous = self.index.get(key)
                    if previous and previous != p["id"]:
                        raise ValueError(f"Account alias is mapped to multiple people: {system}/{acct}")
                    self.index[key] = p["id"]

    def resolve(self, system, account, display=None, bot=False):
        """返回人物 id；没见过的账号自动登记。"""
        account = (account or "").strip()
        if not account:
            return "slackbot"
        key = (system, _account_key(account))
        if key in self.index:
            pid = self.index[key]
        else:
            pid = f"{system[:2]}-{_slug(account)}"
            if pid not in self.people:
                is_bot = bot or account.endswith("[bot]") or account.casefold().endswith("-bot")
                hue = int(hashlib.md5(account.encode()).hexdigest()[:4], 16) % 360
                accounts = dict(slack=[], femini=[], github=[], hubspot=[])
                accounts[system] = [account]
                name = display or (account if is_bot else looks_like_name(account))
                self.people[pid] = dict(
                    id=pid, name=name, short=name.split(" ")[0][:18],
                    kind="bot" if is_bot else "human", color=f"hsl({hue} 55% 68%)", **accounts)
                self.dynamic.append(pid)
            self.index[key] = pid
        if pid not in self.used:
            self.used.append(pid)
        return pid

    def touch(self, pid):
        if pid not in self.used:
            self.used.append(pid)
        return pid

    def campaign_roles(self, campaign_id):
        """Load campaign-specific assignments from the private config directory."""
        if not _ROLES_FILE.exists():
            return {}
        campaigns = json.loads(_ROLES_FILE.read_text(encoding="utf-8")).get("campaigns", {})
        assignments = campaigns.get(str(campaign_id), [])
        result = {}
        for item in assignments:
            pid = item.get("person_id")
            role_zh, role_en = item.get("role_zh"), item.get("role_en")
            if pid not in self.people:
                raise ValueError(f"Unknown person in campaign role assignment: {pid}")
            if self.people[pid].get("kind") != "human":
                raise ValueError(f"Campaign roles can only be assigned to colleagues: {pid}")
            if not isinstance(role_zh, str) or not role_zh.strip() or not isinstance(role_en, str) or not role_en.strip():
                raise ValueError(f"Campaign role assignment requires Chinese and English labels: {pid}")
            role = {"zh": role_zh.strip(), "en": role_en.strip()}
            if role not in result.setdefault(pid, []):
                result[pid].append(role)
        return result

    def job_title(self, pid, lang):
        title = self.people[pid].get("job_title", {})
        if isinstance(title, str):
            return title.strip() or None
        if not isinstance(title, dict):
            return None
        value = title.get(lang)
        return value.strip() if isinstance(value, str) and value.strip() else None
