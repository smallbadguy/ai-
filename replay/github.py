"""只读取 GitHub：按 click URL 编号找相关单子，拉时间线、评论和关联的代码合并请求。

取数方式（自动选择，输出完全一样）：
  1. 有令牌（环境变量 GITHUB_TOKEN，或 $REPLAY_SECRETS/github.env 里的 GITHUB_TOKEN=...）→ 直接调 REST 接口
  2. 没有令牌但本机有 gh 并已登录 → 用 gh api
  3. 都没有 → 不取数，沿用 raw/github 里已有的缓存（比如从别的机器同步过来的）

只发 GET 请求：不评论、不改标签、不写任何东西。
落盘（raw/ 已在 .gitignore，评论原文不入库）：
  raw/github/<仓库>/issue_<号>.json   单子 + 事件 + 评论
  raw/github/<仓库>/pr_<号>.json      代码合并请求 + 评审 + 请求评审记录
  raw/campaigns/<编号>/github.json    这个案子的搜索结果与入选清单
"""
import json
import os
import pathlib
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from .util import RAW, raw_dir, now_z, num_re, read_json, write_json

ORG = "feed-mob"
CAP = 8  # 最多展开几张单子（按最近更新排序）；其余只在核对清单里列出


def _token():
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        return tok.strip()
    secrets = os.environ.get("REPLAY_SECRETS")
    if secrets:
        f = pathlib.Path(secrets) / "github.env"
        if f.exists():
            for line in f.read_text().splitlines():
                if line.startswith("GITHUB_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


class GH:
    def __init__(self):
        self.token = _token()
        self.mode = "token" if self.token else ("gh" if shutil.which("gh") else None)

    def available(self):
        return self.mode is not None

    # ── 取数 ──
    def _req(self, url):
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "campaign-replay-readonly"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8") or "null"), r.headers.get("Link", "")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"GitHub {e.code}: {url.split('api.github.com')[-1][:120]}") from None

    def get(self, path, params=None, paginate=False):
        params = dict(params or {})
        if self.mode == "token":
            if paginate:
                params.setdefault("per_page", 100)
            url = "https://api.github.com/" + path + ("?" + urllib.parse.urlencode(params) if params else "")
            out = []
            while url:
                data, link = self._req(url)
                if not paginate:
                    return data
                out.extend(data if isinstance(data, list) else [data])
                url = next((p.split(";")[0].strip(" <>") for p in link.split(",") if 'rel="next"' in p), None)
            return out
        if self.mode == "gh":
            cmd = ["gh", "api", "-X", "GET", path]
            for k, v in params.items():
                cmd += ["-f", f"{k}={v}"]
            if paginate:
                cmd += ["--paginate", "--slurp"]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
            if r.returncode:
                raise RuntimeError(f"gh api {path} 失败：{r.stderr.strip()[:200]}")
            data = json.loads(r.stdout or "null")
            if paginate:
                flat = []
                for page in data:
                    flat.extend(page if isinstance(page, list) else [page])
                return flat
            return data
        raise RuntimeError("没有可用的 GitHub 凭证（既没有令牌也没有 gh）")


def _slug(repo):
    return repo.replace("/", "__")


def issue_file(repo, num):
    return RAW / "github" / _slug(repo) / f"issue_{num}.json"


def pr_file(repo, num):
    return RAW / "github" / _slug(repo) / f"pr_{num}.json"


def _lite(user):
    return {"login": (user or {}).get("login")} if user else None


def pull_issue(gh, repo, num, updated_hint=None):
    path = issue_file(repo, num)
    cached = read_json(path)
    if cached and updated_hint and cached["issue"].get("updated_at") == updated_hint:
        return cached
    base = f"repos/{repo}/issues/{num}"
    issue = gh.get(base)
    events = gh.get(base + "/events", paginate=True)
    comments = gh.get(base + "/comments", paginate=True)
    timeline = gh.get(base + "/timeline", paginate=True)
    prs = []
    for e in timeline:
        if e.get("event") == "cross-referenced":
            src = (e.get("source") or {}).get("issue") or {}
            if src.get("pull_request") and (src.get("repository") or {}).get("full_name", "").startswith(ORG + "/"):
                key = (src["repository"]["full_name"], src["number"])
                if key not in prs:
                    prs.append(key)
    doc = {"fetched_at": now_z(), "repo": repo, "number": num, "issue": issue, "events": events,
           "comments": comments, "prs": [list(p) for p in prs]}
    write_json(path, doc)
    return doc


def pull_pr(gh, repo, num):
    path = pr_file(repo, num)
    cached = read_json(path)
    if cached and cached["pull"].get("state") == "closed":
        return cached  # 已关闭/已合并的不会再变
    base = f"repos/{repo}/pulls/{num}"
    p = gh.get(base)
    reviews = gh.get(base + "/reviews", paginate=True)
    tl = gh.get(f"repos/{repo}/issues/{num}/timeline", paginate=True)
    reqs = [{"event": e["event"], "created_at": e.get("created_at"), "actor": _lite(e.get("actor")),
             "requested_reviewer": _lite(e.get("requested_reviewer"))}
            for e in tl if e.get("event") in ("review_requested", "review_request_removed", "ready_for_review")]
    pull = {k: p.get(k) for k in ("number", "title", "state", "draft", "merged", "merged_at", "created_at",
                                    "closed_at", "additions", "deletions", "changed_files", "commits", "html_url")}
    pull["body"] = (p.get("body") or "")[:600]
    pull["user"] = _lite(p.get("user"))
    pull["merged_by"] = _lite(p.get("merged_by"))
    pull["requested_reviewers"] = [_lite(u) for u in p.get("requested_reviewers", [])]
    doc = {"fetched_at": now_z(), "repo": repo, "number": num, "pull": pull,
           "reviews": [{"user": _lite(r.get("user")), "state": r.get("state"), "submitted_at": r.get("submitted_at"),
                        "body": (r.get("body") or "")[:600]} for r in reviews],
           "requests": reqs}
    write_json(path, doc)
    return doc


def discover(gh, n):
    """按编号搜 GitHub，返回搜索结果（精简）。只有标题或正文真的出现这个编号的才算「相关」。"""
    pat = num_re(n)
    items = []
    for page in (1, 2):
        data = gh.get("search/issues", {"q": f"{n} org:{ORG}", "per_page": 100, "page": page})
        items += data.get("items", [])
        if len(data.get("items", [])) < 100:
            break
    hits = []
    for it in items:
        repo = it["repository_url"].split("/repos/")[1]
        text = f"{it.get('title', '')}\n{it.get('body') or ''}"
        hits.append({"repo": repo, "number": it["number"], "title": it.get("title"), "state": it.get("state"),
                     "is_pr": "pull_request" in it, "created_at": it.get("created_at"),
                     "updated_at": it.get("updated_at"), "url": it.get("html_url"),
                     "user": (it.get("user") or {}).get("login"), "primary": bool(pat.search(text))})
    return hits


def collect(n, log=print):
    """案子的 GitHub 部分。返回状态字典；没有凭证时不报错，沿用缓存并写明。"""
    gh = GH()
    mpath = raw_dir(n) / "github.json"
    old = read_json(mpath)
    if not gh.available():
        log("GitHub：没有凭证（令牌或 gh），沿用已有缓存")
        return {"status": "skipped", "reason": "no-credentials", "asof": (old or {}).get("queried_at"),
                "have_cache": bool(old)}
    hits = discover(gh, n)
    prim = sorted([h for h in hits if h["primary"] and not h["is_pr"]], key=lambda h: h["updated_at"], reverse=True)
    selected, skipped = prim[:CAP], prim[CAP:]
    prs = []
    for h in selected:
        doc = pull_issue(gh, h["repo"], h["number"], h["updated_at"])
        for pr in doc["prs"]:
            if tuple(pr) not in prs:
                prs.append(tuple(pr))
        log(f"  单子 {h['repo']}#{h['number']}：事件 {len(doc['events'])}，评论 {len(doc['comments'])}")
    for h in [h for h in hits if h["primary"] and h["is_pr"]][:CAP]:
        if (h["repo"], h["number"]) not in prs:
            prs.append((h["repo"], h["number"]))
    for repo, num in prs:
        pull_pr(gh, repo, num)
        log(f"  代码合并 {repo}#{num}")
    manifest = {"queried_at": now_z(), "mode": gh.mode, "query": f"{n} org:{ORG}", "hits_total": len(hits),
                "selected": [{"repo": h["repo"], "number": h["number"]} for h in selected],
                "prs": [{"repo": r, "number": x} for r, x in prs],
                "skipped": [{k: h[k] for k in ("repo", "number", "title", "url", "state")} for h in skipped],
                "mentions_only": [{k: h[k] for k in ("repo", "number", "title", "url")}
                                  for h in hits if not h["primary"] and not h["is_pr"]][:50]}
    write_json(mpath, manifest)
    return {"status": "ok", "mode": gh.mode, "asof": manifest["queried_at"], "issues": len(selected),
            "prs": len(prs), "skipped": len(skipped)}
