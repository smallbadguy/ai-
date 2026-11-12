"""Source-read labels describe collection, not whether attribution kept an event."""
from collections import Counter

NAMES = {
    'slack': ('Slack', 'Slack'),
    'hubspot': ('HubSpot 工单', 'HubSpot tickets'),
    'email': ('Gmail 邮件', 'Gmail'),
    'meeting': ('Fireflies 会议', 'Fireflies meetings'),
}
KINDS = {'slack': 'slack', 'hubspot': 'tickets', 'email': 'emails', 'meeting': 'meetings'}


def receipts(feed_state):
    """Copy only collection receipts, never credentials or raw upstream errors."""
    return {sid: {key: (feed_state.get(kind) or {}).get(key) for key in ('ok', 'as_of')}
            for sid, kind in KINDS.items()}


def _status(scan, direct):
    if scan.get('ok') is False or scan.get('error'):
        return 'error'
    if scan.get('ok') is True and scan.get('as_of'):
        return 'ok'
    if not direct:
        fields = ('listed', 'fetched', 'failed')
        if all(type(scan.get(k)) is int and scan[k] >= 0 for k in fields):
            return 'limited' if scan['failed'] else 'ok'
    # A preserved as_of alone is not proof: refresh failures keep old timestamps.
    return 'unknown'


def annotate(case, corpus, lang):
    """Add label-only metadata; leave events, layout and attribution unchanged."""
    if not corpus:
        return case
    z = lang == 'zh'
    stats = corpus.get('stats') or {}
    scans = stats.get('source_scan') or {}
    legacy = stats.get('brain_scan') or {}
    direct = corpus.get('source') == 'direct'
    counts = Counter(s for event in case.get('events') or []
                     for s in (event.get('srcs') or [event.get('src')]))
    statuses = {}
    for sid, names in NAMES.items():
        scan = scans.get(sid) or (stats.get('meeting_scan') if sid == 'meeting' else
                                  legacy.get(KINDS[sid])) or {}
        state = _status(scan, direct)
        name = names[0 if z else 1]
        if sid == 'meeting' and not direct:
            name = 'Fireflies / Teams 会议' if z else 'Fireflies / Teams meetings'
        word = {'ok': ('已读', 'Read'), 'error': ('读取失败', 'Read failed'),
                'limited': ('读取不完整', 'Partially read'), 'unknown': ('读取待确认', 'Read unconfirmed')}[state][0 if z else 1]
        if state == 'ok':
            note = (f'已读取；本回放收录 {counts[sid]} 条记录。' if z else
                    f'Read; {counts[sid]} records included in this replay.')
            if scan.get('as_of'):
                note += (f'副本截至 {scan["as_of"]}。' if z else f' Snapshot as of {scan["as_of"]}.')
        elif state == 'error':
            note = ('最近一次读取失败，不能标记为已读；已有副本和回放内容保留。' if z else
                    'The latest read failed; existing snapshots and replay content are retained.')
        elif state == 'limited':
            note = '部分记录未能读取。' if z else 'Some records could not be read.'
        else:
            note = '缺少读取完成记录，不能标记为已读。' if z else 'No read-completion receipt is available.'
        statuses[sid] = dict(status=state, name=name, word=word, note=note)
    case['source_reads'] = statuses
    for source in case.get('sources') or []:
        state = statuses.get(source.get('id'))
        if state:
            source['status'] = 'ok' if state['status'] == 'ok' else 'limited'
    for pill in case.get('charter') or []:
        title = pill.get('t') or ''
        sid = next((sid for sid, prefix in (('email', 'Gmail'), ('hubspot', 'HubSpot'), ('slack', 'Slack'))
                    if title.startswith(prefix)), None)
        if sid:
            item = statuses[sid]
            pill['ok'] = item['status'] == 'ok'
            if pill['ok']:
                name = 'Slack 副本' if z and sid == 'slack' and not direct else item['name']
                pill['t'] = name + ('已读' if z else ' read')
            else:
                pill['t'] = f'{item["name"]} {item["word"]}'
    return case
