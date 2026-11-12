"""活动（campaign）这一层：全部在跑的活动清单（给下拉列表用），以及一个活动下面有哪些链接。

只调用后台的读取类工具（list_campaigns、get_campaign、list_click_urls）。
需要 REPLAY_SECRETS 指向凭证目录。
"""
import time

from . import collect_mcp, inventory, scope, util
from .util import camp_dir, list_file, now_z, read_json, valid_number, write_json

MAX_LINKS = 12  # 一个活动下最多展开几条链接；其余写进核对清单
LIST_MAX_AGE = 24 * 3600


def refresh_list():
    """重新取全部在跑的活动清单。成功才覆盖旧清单。"""
    f = collect_mcp.mcp_client.femini()
    camps = f.call("list_campaigns", {}).get("campaigns") or []
    camps = [{"id": int(c["id"]), "name": c["name"]} for c in camps if c.get("id") and c.get("name")]
    if not camps:
        raise RuntimeError("后台返回的活动清单是空的")
    camps.sort(key=lambda c: c["name"].casefold())
    write_json(list_file(), {"queried_at": now_z(), "campaigns": camps})
    return {"count": len(camps)}


def load_list():
    return read_json(list_file(), {}) or {}


def list_is_stale():
    d = load_list()
    if not d.get("queried_at"):
        return True
    from datetime import datetime, timezone
    t = datetime.strptime(d["queried_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return time.time() - t.timestamp() > LIST_MAX_AGE


def find(cid):
    """清单里的某个活动；清单还没有或活动不在清单里返回 None。"""
    cid = int(valid_number(cid))
    return next((c for c in load_list().get("campaigns", []) if c["id"] == cid), None)


def vendor_options(cid, refresh=False):
    """Cached options on list pages; explicit refresh only reads backend metadata."""
    doc = inventory.refresh(cid, enrich=True) if refresh else inventory.cached(cid)
    return doc.get('vendors') or []


def validate_vendor(cid, key):
    return inventory.validate_vendor(valid_number(cid), key)


def _store_inventory(cid, found, current):
    names = sorted({str(e['name']) for e in found['links']}, key=lambda s: (len(s), s), reverse=True)
    camp = found['campaign']
    if not names:
        why = '已在后台停用，也没有以前取到的链接' if camp.get('active') is False else '下面没有在用的链接'
        raise RuntimeError(f"活动「{camp['name']}」{why}，没有可回放的内容")
    base = dict(queried_at=found['queried_at'], campaign=camp, links=names[:MAX_LINKS],
                links_not_expanded=names[MAX_LINKS:], current_links=list(current),
                vendors=found['vendors'], history_complete=found['history_complete'],
                coverage=found['coverage'], inventory_errors=found.get('errors') or {})
    if not current:
        base['links_reused'] = True
    write_json(util.base_camp_dir(cid) / 'campaign.json', base)
    vendor = scope.vendor_for(cid)
    if vendor:
        option = validate_vendor(cid, vendor)
        selected = option['links']
        doc = dict(base, links=selected[:MAX_LINKS], links_not_expanded=selected[MAX_LINKS:],
                   vendor_id=vendor, vendor=option)
        write_json(camp_dir(cid) / 'campaign.json', doc)
        return doc
    return base


def discover(cid, log=print):
    """Merge current links with observed history, then select the requested vendor."""
    cid = valid_number(cid)
    f = collect_mcp.mcp_client.femini()
    try:
        camp = f.call('get_campaign', {'id':int(cid)}).get('campaign')
    except Exception as exc:
        raise RuntimeError(f'后台查不到活动 {cid}：{str(exc)[:200]}') from None
    if not camp:
        raise RuntimeError(f'后台查不到活动 {cid}')
    result = f.call('list_click_urls', {'campaign_id':int(cid)})
    listed = (result.get('click_urls') if isinstance(result, dict) else result) or []
    found = inventory.refresh(cid, client=f, campaign=camp, listed=listed,
                              enrich=bool(scope.vendor_for(cid)))
    doc = _store_inventory(cid, found, [str(c['name']) for c in listed])
    log(f"活动 {cid} · {camp['name']}：链接 {len(doc['links'])} 条")
    return doc


def update_inventory(cid):
    """After link collection, incorporate its fresh partner/status data without more calls."""
    base = read_json(util.base_camp_dir(cid) / 'campaign.json', {}) or {}
    if not base.get('campaign'):
        return None
    current = base.get('current_links')
    if current is None:
        current = base.get('links') or []
    found = inventory.refresh(cid, campaign=base['campaign'], listed=[{'name':n} for n in current])
    return _store_inventory(cid, found, current)


def info(cid):
    return read_json(camp_dir(cid) / "campaign.json")


def inactive(cid):
    """后台把这个活动停用了（按最近一次取活动信息时的状态）。没取过、或后台没给这个字段，都当没停用。"""
    return ((info(cid) or {}).get("campaign") or {}).get("active") is False


def links(cid):
    """这个活动要展开的链接。还没有取过就先取。"""
    d = info(cid) or discover(cid, log=lambda *_: None)
    return d["links"]
