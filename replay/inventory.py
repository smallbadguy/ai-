"""Read-only backend inventory plus previously observed click URLs.

The backend's list omits paused URLs. Never pretend that current-and-known
history is an exhaustive historical enumeration, and never infer a pause merely
because an ID disappeared from the list.
"""
import fcntl
import hashlib

from . import scope, util


def _read(cid):
    return util.read_json(util.base_camp_dir(cid) / 'inventory.json', {}) or {}


def _click(doc):
    return (doc.get('click_url') or doc) if isinstance(doc, dict) else {}


def _owner(doc):
    cu = _click(doc)
    camp = cu.get('campaign') or doc.get('campaign') or {}
    return str(camp.get('id') or cu.get('campaign_id') or '')


def vendor_key(partner):
    if partner.get('id'):
        try:
            return scope.valid_vendor(str(partner['id']))
        except ValueError:
            pass
    name = (partner.get('name') or '').strip()
    return 'v-' + hashlib.sha256(name.casefold().encode()).hexdigest()[:12] if name else None


def _entry(name, cu, listed=False, fetched_at=None):
    partner = cu.get('partner') or {}
    if not isinstance(partner, dict):
        partner = {}
    key = vendor_key(partner)
    approved = [c for c in cu.get('proposed_changes') or []
                if c.get('approve_status') == 'approved' and c.get('new_state')
                and (util.iso(c.get('new_state_effective_at') or c.get('updated_at') or c.get('created_at')) or '') <= util.now_z()]
    approved.sort(key=lambda c: (util.iso(c.get('new_state_effective_at') or c.get('updated_at') or c.get('created_at')) or '', str(c.get('id') or '')))
    latest = approved[-1] if approved else {}
    state = str(latest.get('new_state') or '').casefold()
    paused = state in ('paused', 'pause', 'stopped', 'inactive', 'disabled')
    active = False if paused and not listed else True if listed and not paused else None
    return dict(name=str(name), vendor_id=key, vendor_name=(partner.get('name') or '').strip(),
                active=active, status='paused' if active is False else 'active' if active else 'unknown',
                state_effective_at=util.iso(latest.get('new_state_effective_at')),
                created_at=util.iso(cu.get('created_at')), fetched_at=fetched_at,
                listed_now=bool(listed), state_conflict=bool(paused and listed))


def options_from_entries(entries):
    groups = {}
    for entry in entries:
        key = entry.get('vendor_id')
        if not key:
            continue
        group = groups.setdefault(key, dict(id=key, name=entry.get('vendor_name') or key, links=[], states=[]))
        group['links'].append(str(entry['name']))
        group['states'].append(entry.get('active'))
    out = []
    for group in groups.values():
        states = group.pop('states')
        group['links'] = sorted(set(group['links']), key=lambda n: (len(n), n), reverse=True)
        group['active'] = True if True in states else False if states and all(s is False for s in states) else None
        out.append(group)
    return sorted(out, key=lambda v: (v['name'].casefold(), v['id']))


def cached(cid):
    """No network or global scans on the homepage's polling path."""
    doc = _read(cid)
    if doc:
        return doc
    previous = util.read_json(util.base_camp_dir(cid) / 'campaign.json', {}) or {}
    entries = []
    for name in (previous.get('links') or []) + (previous.get('links_not_expanded') or []):
        record = util.read_json(util.raw_dir(name) / 'femini.json', {}) or {}
        if record and _owner(record) in ('', str(cid)):
            entries.append(_entry(name, _click(record), fetched_at=record.get('queried_at')))
    return dict(campaign=previous.get('campaign') or {}, links=entries,
                vendors=options_from_entries(entries), history_complete=False,
                coverage='current_and_known_history', queried_at=previous.get('queried_at'))


def refresh(cid, client=None, campaign=None, listed=None, enrich=False):
    """Serialize inventory merges so concurrent reads never discard known history."""
    cid = util.valid_number(cid)
    base = util.base_camp_dir(cid)
    base.mkdir(parents=True, exist_ok=True)
    with open(base / 'inventory.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return _refresh(cid, client, campaign, listed, enrich)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _refresh(cid, client=None, campaign=None, listed=None, enrich=False):
    """The only remote calls here are get_campaign/list_click_urls/get_click_url."""
    if campaign is None or listed is None:
        if client is None:
            from .collect_mcp import mcp_client
            client = mcp_client.femini()
        if campaign is None:
            campaign = client.call('get_campaign', {'id':int(cid)}).get('campaign') or {}
        if listed is None:
            result = client.call('list_click_urls', {'campaign_id':int(cid)})
            listed = (result.get('click_urls') if isinstance(result, dict) else result) or []
    if not campaign:
        raise RuntimeError('Campaign was not found in FeedMob Admin')
    previous = util.read_json(util.base_camp_dir(cid) / 'campaign.json', {}) or {}
    known = {str(e['name']):e for e in _read(cid).get('links', [])}
    details = {}
    for path in (util.RAW / 'links').glob('*/femini.json'):
        doc = util.read_json(path, {}) or {}
        if _owner(doc) == cid:
            name = str(_click(doc).get('name') or path.parent.name)
            if name.isdigit():
                details[name] = doc
                known.setdefault(name, {'name':name})
    for name in (previous.get('links') or []) + (previous.get('links_not_expanded') or []):
        known.setdefault(str(name), {'name':str(name)})
    current = {str(c['name']):c for c in listed if isinstance(c, dict) and str(c.get('name') or '').isdigit()}
    known.update({name:known.get(name) or {'name':name} for name in current})
    errors = {}
    entries = []
    for name, old in sorted(known.items()):
        record = details.get(name) or util.read_json(util.raw_dir(name) / 'femini.json', {}) or {}
        cu = _click(record)
        if current.get(name, {}).get('partner'):
            cu = {**cu, **current[name]}
        fetched = record.get('queried_at')
        if enrich and client is not None:
            try:
                reply = client.call('get_click_url', {'name':name})
                if _owner(reply) not in ('', cid):
                    errors[name] = 'Click URL belongs to a different campaign'
                    continue
                got = _click(reply)
                if got and got.get('name') and got.get('partner'):
                    cu, fetched = got, util.now_z()
            except Exception as exc:
                errors[name] = type(exc).__name__
        if record and _owner(record) not in ('', cid):
            errors[name] = 'Cached click URL belongs to a different campaign'
            continue
        entry = _entry(name, cu, name in current, fetched)
        if not entry.get('vendor_id') and old.get('vendor_id'):
            entry.update(vendor_id=old['vendor_id'], vendor_name=old.get('vendor_name') or old['vendor_id'])
            if name not in current:
                entry.update(active=old.get('active'), status=old.get('status') or 'unknown',
                             state_effective_at=old.get('state_effective_at'))
        entries.append(entry)
    doc = dict(queried_at=util.now_z(), campaign=campaign, links=entries,
               vendors=options_from_entries(entries), history_complete=False,
               coverage='current_and_known_history', errors=errors)
    util.write_json(util.base_camp_dir(cid) / 'inventory.json', doc)
    return doc


def validate_vendor(cid, key):
    key = scope.valid_vendor(key)
    if key is None:
        return None
    option = next((v for v in cached(cid).get('vendors', []) if v['id'] == key), None)
    if option is None:
        raise ValueError('The vendor is not registered for this campaign')
    return option
