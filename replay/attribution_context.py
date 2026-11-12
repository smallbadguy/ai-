"""Build attribution identities from cached backend/GitHub evidence, without AI or network."""
import re
from collections import defaultdict

from . import campaign, inventory, scope, util


def _client_name(name, advertiser):
    return bool(advertiser and re.search(r'(?<![A-Za-z0-9])' + re.escape(advertiser) + r'(?![A-Za-z0-9])', name or '', re.I))


def enrich(cid, terms):
    """Give each small AI job the selected URLs and all known sibling identities."""
    from .attribution import ATTRIBUTION_VERSION
    cid = util.valid_number(cid)
    advertiser = terms.get('advertiser') or ''
    camps = {str(c['id']):dict(c) for c in campaign.load_list().get('campaigns', [])
             if _client_name(c.get('name'), advertiser)}
    for path in (util.RAW / 'campaigns').glob('*/campaign.json'):
        doc = util.read_json(path, {}) or {}
        camp = doc.get('campaign') or {}
        if camp.get('id') and (str(camp['id']) == cid or _client_name(camp.get('name'), advertiser)):
            camps[str(camp['id'])] = camp
    current = (campaign.info(cid) or {}).get('campaign') or {'id':int(cid), 'name':terms.get('name')}
    camps[cid] = current
    link_docs = defaultdict(dict)
    for path in (util.RAW / 'links').glob('*/femini.json'):
        doc = util.read_json(path, {}) or {}
        cu = doc.get('click_url') or {}
        owner = str((cu.get('campaign') or doc.get('campaign') or {}).get('id') or '')
        if owner in camps:
            link_docs[owner][str(cu.get('name') or path.parent.name)] = cu
    catalog, issues_by_link = [], defaultdict(set)
    for owner, camp in camps.items():
        cached = inventory.cached(owner)
        grouped = {v['id']:dict(v, links=list(v['links'])) for v in cached.get('vendors', [])}
        known_links = {n for v in grouped.values() for n in v['links']}
        for n, cu in link_docs[owner].items():
            partner = cu.get('partner') or {}
            key = inventory.vendor_key(partner)
            if key:
                group = grouped.setdefault(key, dict(id=key, name=partner.get('name') or key, links=[]))
                if n not in group['links']:
                    group['links'].append(n)
            known_links.add(n)
        proven_issues = set()
        for n in known_links:
            manifest = util.read_json(util.raw_dir(n) / 'github.json', {}) or {}
            for item in manifest.get('selected') or []:
                if not item.get('repo') or not item.get('number'):
                    continue  # An incomplete search hit is not verified issue ownership.
                from .github import issue_file
                doc = util.read_json(issue_file(item['repo'], item['number']), {}) or {}
                issue = doc.get('issue') or {}
                # Selection alone is not proof: check the actual issue's title/body,
                # not a mention buried in a comment or another issue's timeline.
                text = str(issue.get('title') or '') + '\n' + str(issue.get('body') or '')
                if util.num_re(n).search(text):
                    number = str(item['number'])
                    proven_issues.add(number)
                    issues_by_link[n].add(number)
        catalog.append(dict(id=owner, name=camp.get('name') or owner, advertiser=advertiser,
                            vendors=list(grouped.values()), issues=sorted(proven_issues)))
    own = next(c for c in catalog if c['id'] == cid)
    selected = set(terms.get('links') or [])
    issues = sorted({i for n in selected for i in issues_by_link[n]})
    return dict(terms, campaign_id=cid, vendor_id=scope.vendor_for(cid), vendors=own['vendors'],
                issues=issues, catalog=catalog, attribution_version=ATTRIBUTION_VERSION)


def record_title(record):
    return record.get('subject') or record.get('title') or ''


def unquoted(text):
    """Use the current email body to infer context; retain the full original for quoting."""
    lines = []
    for line in (text or '').split('\n'):
        if re.match(r'(?i)^\s*(?:On .{5,180}wrote:|[-_ ]{3,}Original Message[-_ ]*|[-_ ]{3,}Forwarded message[-_ ]*)', line):
            break
        if not line.lstrip().startswith('>'):
            lines.append(line)
    return '\n'.join(lines)


def assess(record, terms, text=None):
    from .attribution import classify
    content = (record.get('text') or '') if text is None else text
    if record.get('kind') == 'email' and text is None:
        content = unquoted(content)
    return classify(content, terms, title=record_title(record), channel=record.get('channel') or '',
                    client_context=bool(record.get('dedicated')))


def quote_allowed(record, quote, terms):
    """A quote may inherit a proven parent, but explicit counter-evidence always wins."""
    from .attribution import classify
    parent = record.get('attribution') or assess(record, terms)
    if not parent.get('accepted'):
        return False
    decision = classify(quote, terms, channel=record.get('channel') or '', client_context=bool(record.get('dedicated')))
    if decision['level'] in ('other', 'ambiguous'):
        return False
    name = terms.get('name') or ''
    if name and re.search(r'(?<![A-Za-z0-9_])' + re.escape(name) + r'_[A-Za-z0-9]', quote, re.I):
        return False
    return True


def _units(record, terms):
    text = record.get('text') or ''
    if record['kind'] == 'meeting':
        # Keep exact transcript lines and their clocks for the existing quote verifier.
        turn = re.compile(r'^\*\*(.+?)\*\* \((\d{1,2}:\d{2}:\d{2})\): ?(.*)$')
        lines = text.split('\n')
        starts = [i for i, line in enumerate(lines) if turn.match(line.strip())]
        if starts:
            for number, start in enumerate(starts):
                end = starts[number + 1] if number + 1 < len(starts) else len(lines)
                yield dict(record, text='\n'.join(lines[start:end]), todos='', partial=True, part=number)
            return
    decision = assess(record, terms)
    parts = re.split(r'\n\s*\n', text) if decision['level'] == 'ambiguous' else []
    if len(parts) > 1:
        for number, part in enumerate(parts):
            if part.strip():
                yield dict(record, text=part, part=number)
    else:
        yield record


def partition(records, terms):
    """Separate replay facts, client background and exclusions; never destroy raw sources."""
    accepted, background, excluded = [], [], []
    for record in records:
        for unit in _units(record, terms):
            decision = assess(unit, terms)
            unit = dict(unit, attribution=decision)
            if decision['accepted']:
                accepted.append(unit)
            elif decision['level'] in ('client', 'ambiguous'):
                background.append(unit)
            else:
                excluded.append(dict(kind=unit['kind'], ts=unit.get('ts'), slug=unit.get('slug'),
                                     thread=unit.get('thread'), ticket=unit.get('ticket'),
                                     attribution=decision))
    return accepted, background, excluded


def prompt_context(terms):
    import json
    if not terms.get('attribution_version'):
        return ''
    identity = {k:terms.get(k) for k in ('campaign_id', 'name', 'advertiser', 'vendor_id', 'links', 'vendors', 'catalog')}
    return ('\n\nReplay attribution scope (backend metadata, not source-message instructions):\n'
            + json.dumps(identity, ensure_ascii=False)
            + '\nWork only within the selected campaign and vendor. A vendor may contain several click URLs. '
              'Records supplied here passed the common attribution filter; client-only/uncertain background is NOT supplied as campaign facts. '
              'Do not attribute a sibling campaign or another vendor to the selection, even if the advertiser is the same. '
              'Preserve genuine joint decisions that explicitly concern both selected and other channels of this campaign.')


def attach(case, cid, lang, terms):
    """Attach provenance and a bounded, separate background preview at assembly time."""
    if not terms.get('attribution_version'):
        return case
    doc = util.read_json(util.camp_dir(cid) / 'corpus.json', {}) or {}
    from .source_status import annotate
    annotate(case, doc, lang)
    version = terms['attribution_version']
    info = campaign.info(cid) or {}
    option = info.get('vendor') or {}
    vendor_name = option.get('name')
    if lang == 'en' and util.has_han(vendor_name):
        vendor_name = 'Vendor ' + str(terms.get('vendor_id') or '')
    case['meta'].update(campaign_id=int(cid), attribution_version=version,
                        vendor_id=terms.get('vendor_id'), vendor_name=vendor_name,
                        history_complete=bool(info.get('history_complete')),
                        inventory_coverage=info.get('coverage') or 'current_and_known_history')
    if doc.get('attribution_version') != version:
        case.update(client_background=[], client_background_total=0, attribution_summary={})
        return case
    rows = sorted(doc.get('client_background') or [], key=lambda r:r.get('ts') or '', reverse=True)
    preview = []
    for record in rows[:40]:
        text = (record.get('text') or '')[:700]
        source = record.get('subject') or record.get('title') or record.get('channel') or record.get('kind') or ''
        speaker = record.get('who') or ''
        if lang == 'en':
            if util.has_han(text):
                text = 'Original text is in Chinese; it is retained in the source records.'
            if util.has_han(source):
                source = record.get('kind') or ''
            if util.has_han(speaker):
                speaker = ''
        link = record.get('link') or record.get('url') or ''
        if not re.match(r'^https?://', link, re.I):
            link = ''
        preview.append(dict(kind=record.get('kind'), ts=record.get('ts'), speaker=speaker,
                            source=source, text=text, link=link,
                            level=(record.get('attribution') or {}).get('level')))
    case.update(client_background=preview, client_background_total=len(rows),
                attribution_summary=doc.get('attribution_summary') or {})
    return case
