"""Conservative, deterministic attribution for replay records.

The module deliberately uses only evidence supplied in ``terms``. In
particular, campaign context words are not treated as vendors and numbers are
not treated as click URLs unless they are known and explicitly labeled.
"""
import re


ATTRIBUTION_VERSION = "1.0"

_EMAIL_RE = re.compile(r"(?i)[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}")
_URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>]+")
_DOMAIN_RE = re.compile(r"(?i)(?<![\w@])(?:[a-z0-9-]+\.)+[a-z]{2,}(?![\w])")


def _text(value):
    return "" if value is None else str(value)


def _items(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _id(value):
    if value is None:
        return ""
    return str(value).strip()


def _unique(values):
    result, seen = [], set()
    for value in values:
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _literal_pattern(value, *, campaign=False):
    value = _text(value).strip()
    if not value:
        return None
    body = re.escape(value)
    if not campaign:
        body = re.sub(r"\\\s+", r"\\s+", body)
    # Campaign names use underscores as internal separators; a surrounding
    # underscore therefore means a longer campaign, not a complete-name hit.
    boundary = r"[A-Za-z0-9_]" if campaign else r"[A-Za-z0-9]"
    return re.compile(r"(?<!" + boundary + r")" + body + r"(?!" + boundary + r")", re.I)


def _lexical_surface(value):
    value = _EMAIL_RE.sub(" ", _text(value))
    value = _URL_RE.sub(" ", value)
    return _DOMAIN_RE.sub(" ", value)


def _number_pattern(value):
    value = re.escape(value)
    # IDs are commonly numeric, but retaining token boundaries also supports
    # opaque IDs without allowing a shorter ID to match a longer one.
    return r"(?<![A-Za-z0-9])" + value + r"(?![A-Za-z0-9])"


def _number_surface(value):
    punctuation = str.maketrans({"＃": "#", "：": ":", "／": "/", "－": "-"})
    return _text(value).translate(punctuation)


def _click_url_ids(value, known_ids):
    surface = _EMAIL_RE.sub(" ", _number_surface(value))
    found = set()
    for ident in known_ids:
        token = _number_pattern(ident)
        path = re.compile(r"(?i)(?:^|/)click[_-]?urls?/" + token)
        phrase = re.compile(
            r"(?i)(?<![A-Za-z0-9])click[\s_-]*urls?"
            r"\s*(?:(?:number|no\.?|#|:|=)\s*)?" + token
        )
        labeled_url = re.compile(
            r"(?i)(?<![A-Za-z0-9])(?:url|link)\s*(?:(?:number|no\.?|#|:|=)\s*)?" + token
        )
        if path.search(surface) or phrase.search(surface) or labeled_url.search(surface):
            found.add(ident)
    return found


def _issue_ids(value, known_ids):
    surface = _EMAIL_RE.sub(" ", _number_surface(value))
    found = set()
    for ident in known_ids:
        token = _number_pattern(ident)
        pattern = re.compile(
            r"(?i)(?<![A-Za-z0-9_])(?:GH\s*#|#|issues/|pull/)\s*" + token
        )
        for match in pattern.finditer(surface):
            before = surface[max(0, match.start() - 32):match.start()]
            # HubSpot tickets often use the same #NN syntax as issue references.
            if re.search(r"(?i)\bhub[\s_-]*spot\s*$", before):
                continue
            if re.search(r"(?i)\burl\s*$", before):
                continue
            found.add(ident)
            break
    return found


def _vendor(value):
    if not isinstance(value, dict):
        return None
    name = _text(value.get("name")).strip()
    ident = _id(value.get("id"))
    links = [_id(x) for x in _items(value.get("links")) if _id(x)]
    if not name and not ident and not links:
        return None
    return {"id": ident, "name": name, "links": _unique(links)}


def _entry(value):
    if not isinstance(value, dict):
        return None
    name = _text(value.get("name")).strip()
    ident = _id(value.get("id"))
    if not name and not ident:
        return None
    key = "id:" + ident if ident else "name:" + name.casefold()
    vendors = [v for v in (_vendor(x) for x in _items(value.get("vendors"))) if v]
    issues = [_id(x) for x in _items(value.get("issues")) if _id(x)]
    return {
        "key": key,
        "id": ident,
        "name": name,
        "aliases": [name] if name else [],
        "advertiser": _text(value.get("advertiser")).strip(),
        "vendors": vendors,
        "issues": _unique(issues),
        "current": False,
    }


def _merge_vendors(*groups):
    result = []
    for group in groups:
        for vendor in group:
            marker = ("id", vendor["id"]) if vendor["id"] else ("name", vendor["name"].casefold())
            existing = next((x for x in result if
                             (("id", x["id"]) if x["id"] else ("name", x["name"].casefold())) == marker), None)
            if existing is None:
                result.append({"id": vendor["id"], "name": vendor["name"], "links": list(vendor["links"])})
            else:
                if not existing["name"]:
                    existing["name"] = vendor["name"]
                existing["links"] = _unique(existing["links"] + vendor["links"])
    return result


def _campaigns(terms):
    entries = []
    for raw in _items(terms.get("catalog")):
        entry = _entry(raw)
        if entry is None:
            continue
        existing = next((x for x in entries if x["key"] == entry["key"]), None)
        if existing is None:
            entries.append(entry)
        else:
            existing["aliases"] = _unique(existing["aliases"] + entry["aliases"])
            existing["vendors"] = _merge_vendors(existing["vendors"], entry["vendors"])
            existing["issues"] = _unique(existing["issues"] + entry["issues"])
            if not existing["advertiser"]:
                existing["advertiser"] = entry["advertiser"]
            if not existing["name"]:
                existing["name"] = entry["name"]

    current_name = _text(terms.get("name")).strip()
    current_id = _id(terms.get("campaign_id"))
    by_id = [x for x in entries if current_id and x["id"] == current_id]
    by_name = [x for x in entries if current_name and
               any(a.casefold() == current_name.casefold() for a in x["aliases"])]
    matches = by_id if current_id else by_name
    identity_ambiguous = not current_id and len(matches) > 1
    current = matches[0] if matches else None
    if current is None:
        current = {
            "key": "id:" + current_id if current_id else "name:" + current_name.casefold(),
            "id": current_id,
            "name": current_name,
            "aliases": [current_name] if current_name else [],
            "advertiser": _text(terms.get("advertiser")).strip(),
            "vendors": [],
            "issues": [],
            "current": True,
        }
        entries.append(current)
    else:
        current["current"] = True
        current["aliases"] = _unique(current["aliases"] + ([current_name] if current_name else []))
        if current_name:
            current["name"] = current_name
        if terms.get("advertiser"):
            current["advertiser"] = _text(terms.get("advertiser")).strip()

    current_vendors = [v for v in (_vendor(x) for x in _items(terms.get("vendors"))) if v]
    current["vendors"] = _merge_vendors(current["vendors"], current_vendors)
    current["issues"] = _unique(current["issues"] + [_id(x) for x in _items(terms.get("issues")) if _id(x)])
    return entries, current, identity_ambiguous


def _same_client(campaign, advertiser):
    other = _text(campaign.get("advertiser")).strip()
    return not advertiser or not other or other.casefold() == advertiser.casefold()


def classify(text, terms, *, title="", channel="", client_context=False):
    """Classify a record against a campaign/vendor selection.

    ``terms.links`` and ``terms.issues`` are treated as identifiers belonging
    to the current selection. ``terms.vendors`` and ``terms.catalog`` provide
    cross-campaign evidence when available.
    """
    terms = terms if isinstance(terms, dict) else {}
    entries, current, identity_ambiguous = _campaigns(terms)
    advertiser = _text(terms.get("advertiser")).strip() or current.get("advertiser", "")
    selected_vendor_id = _id(terms.get("vendor_id"))
    selected_vendors = [v for v in current["vendors"] if selected_vendor_id and v["id"] == selected_vendor_id]
    vendor_selection = bool(selected_vendor_id)
    target_vendor_names = {v["name"].casefold() for v in selected_vendors if v["name"]}

    # Campaign and vendor names are checked only in prose/title, not email or
    # URL domains. Channel metadata can establish client context, not a vendor.
    raw_primary = _text(text) + "\n" + _text(title)
    surface = _lexical_surface(raw_primary)
    channel_surface = _lexical_surface(channel)
    evidence = []
    campaign_names = []
    vendor_names = []
    current_campaign_hit = False
    other_campaign_hits = set()
    campaign_spans = []

    for campaign in entries:
        hit_names = []
        for alias in campaign["aliases"]:
            pattern = _literal_pattern(alias, campaign=True)
            if not pattern:
                continue
            for match in pattern.finditer(surface):
                hit_names.append((alias, match.span()))
        if not hit_names:
            continue
        for alias, span in hit_names:
            campaign_spans.append(span)
            evidence.append("campaign-name:" + alias)
            evidence.append(("current-campaign-name:" if campaign["current"] else "other-campaign-name:") + alias)
        if campaign["current"]:
            current_campaign_hit = True
        else:
            other_campaign_hits.add(campaign["key"])
        if campaign["name"]:
            campaign_names.append(campaign["name"])

    current_name = _text(terms.get('name')).strip()
    if current_name:
        longer = re.compile(r'(?<![A-Za-z0-9_])' + re.escape(current_name) + r'_[A-Za-z0-9_-]+', re.I)
        for match in longer.finditer(surface):
            # The backend catalog may omit paused siblings. A longer explicit
            # campaign name is still not this shorter campaign, even then.
            other_campaign_hits.add('longer:' + match.group(0).casefold())
            campaign_spans.append(match.span())
            campaign_names.append(match.group(0))
            evidence.append('other-campaign-name:' + match.group(0))

    client_hit = bool(client_context)
    if client_context:
        evidence.append("client-context")
    client_pattern = _literal_pattern(advertiser)
    if client_pattern and (client_pattern.search(surface) or client_pattern.search(channel_surface)):
        client_hit = True
        evidence.append("client:" + advertiser)

    # Ignore vendor tokens that occur only as part of a full campaign name:
    # the campaign label itself does not prove which vendor was selected.
    vendor_hit_records = []
    for campaign in entries:
        for vendor in campaign["vendors"]:
            if not vendor["name"]:
                continue
            pattern = _literal_pattern(vendor["name"])
            if not pattern:
                continue
            for match in pattern.finditer(surface):
                start, end = match.span()
                embedded = any(a <= start and end <= b for a, b in campaign_spans)
                vendor_hit_records.append((campaign, vendor, match.span(), embedded))

    standalone_vendor_hits = [x for x in vendor_hit_records if not x[3]]
    matched_vendor_names = _unique([x[1]["name"] for x in standalone_vendor_hits])
    vendor_candidates = {}
    target_vendor_hit = False
    for campaign, vendor, span, _embedded in standalone_vendor_hits:
        vendor_name = vendor["name"]
        evidence.append("vendor:" + vendor_name)
        vendor_names.append(vendor_name)
        if campaign["name"]:
            campaign_names.append(campaign["name"])
        if _same_client(campaign, advertiser):
            vendor_candidates.setdefault(vendor_name.casefold(), set()).add(campaign["key"])
        if campaign["current"]:
            if not vendor_selection:
                target_vendor_hit = True
            elif vendor["id"] == selected_vendor_id and vendor_name.casefold() in target_vendor_names:
                target_vendor_hit = True

    # A vendor can also appear as an inventory token in a sibling campaign's
    # name even when that campaign's backend partner has a different name.
    # Keep it as a candidate, not a synonym or a direct attribution.
    for vendor_name in matched_vendor_names:
        pattern = _literal_pattern(vendor_name)
        for campaign in entries:
            if campaign["current"] or not _same_client(campaign, advertiser):
                continue
            if pattern and any(pattern.search(alias) for alias in campaign["aliases"]):
                vendor_candidates.setdefault(vendor_name.casefold(), set()).add(campaign["key"])
                if campaign["name"]:
                    campaign_names.append(campaign["name"])
                    evidence.append("vendor-candidate-name:" + campaign["name"] + ":" + vendor_name)

    # Known URL IDs require an explicit URL/click-URL label or click_urls path.
    current_terms_links = [_id(x) for x in _items(terms.get("links")) if _id(x)]
    link_map = {}

    def add_link(ident, campaign, vendor=None):
        if not ident:
            return
        assoc = (campaign["key"], vendor["id"] if vendor else "", vendor["name"] if vendor else "")
        records = link_map.setdefault(ident, {})
        records[assoc] = (campaign, vendor)

    for campaign in entries:
        for vendor in campaign["vendors"]:
            for ident in vendor["links"]:
                add_link(ident, campaign, vendor)
    for ident in current_terms_links:
        add_link(ident, current, selected_vendors[0] if len(selected_vendors) == 1 else None)

    click_hits = _click_url_ids(raw_primary, set(link_map) | set(current_terms_links))
    current_scoped_link = False
    current_unscoped_link = False
    other_current_vendor_link = False
    other_link_campaigns = set()
    link_vendor_names = []
    for ident in sorted(click_hits):
        evidence.append("click-url:" + ident)
        associations = list(link_map.get(ident, {}).values())
        if ident in current_terms_links:
            current_scoped_link = True
        for campaign, vendor in associations:
            if campaign["name"]:
                campaign_names.append(campaign["name"])
            if vendor and vendor["name"]:
                link_vendor_names.append(vendor["name"])
                evidence.append("vendor-link:" + vendor["name"])
            if campaign["current"]:
                evidence.append("current-campaign-link:" + (campaign["name"] or campaign["key"]))
                if not vendor_selection:
                    current_scoped_link = True
                elif vendor and vendor["id"] == selected_vendor_id:
                    current_scoped_link = True
                elif not vendor:
                    if ident in current_terms_links:
                        current_scoped_link = True
                    else:
                        current_unscoped_link = True
                else:
                    other_current_vendor_link = True
            else:
                other_link_campaigns.add(campaign["key"])
                evidence.append("other-campaign-link:" + (campaign["name"] or campaign["key"]))

    # Issue IDs require an explicit issue-style reference and must be known in
    # the current selection or catalog. HubSpot#NN is intentionally excluded.
    current_terms_issues = [_id(x) for x in _items(terms.get("issues")) if _id(x)]
    issue_map = {}
    for campaign in entries:
        for ident in campaign["issues"]:
            issue_map.setdefault(ident, set()).add(campaign["key"])
    for ident in current_terms_issues:
        issue_map.setdefault(ident, set()).add(current["key"])
    issue_hits = _issue_ids(raw_primary, set(issue_map) | set(current_terms_issues))
    current_scoped_issue = False
    current_unscoped_issue = False
    other_issue_campaigns = set()
    for ident in sorted(issue_hits):
        evidence.append("issue:" + ident)
        for key in issue_map.get(ident, set()):
            campaign = next((x for x in entries if x["key"] == key), None)
            if campaign is None:
                continue
            if campaign["name"]:
                campaign_names.append(campaign["name"])
            if campaign["current"]:
                evidence.append("current-campaign-issue:" + (campaign["name"] or campaign["key"]))
                if ident in current_terms_issues or not vendor_selection:
                    current_scoped_issue = True
                elif len(current["vendors"]) == 1 and current["vendors"][0]["id"] == selected_vendor_id:
                    current_scoped_issue = True
                else:
                    current_unscoped_issue = True
            else:
                other_issue_campaigns.add(key)
                evidence.append("other-campaign-issue:" + (campaign["name"] or key))

    # Resolve exact campaign evidence in the selected vendor scope.
    current_vendor_count = len(current["vendors"])
    selected_name_count = len([v for v in current["vendors"] if v["name"].casefold() in target_vendor_names])
    selected_name_collision = vendor_selection and selected_name_count > 1 and target_vendor_hit
    if not vendor_selection:
        current_direct = current_campaign_hit or current_scoped_link or current_scoped_issue
        current_scope_ambiguous = False
    else:
        selected_is_sole_vendor = current_vendor_count == 1 and len(selected_vendors) == 1
        campaign_name_resolves_vendor = current_campaign_hit and (target_vendor_hit or selected_is_sole_vendor)
        current_direct = current_scoped_link or current_scoped_issue or campaign_name_resolves_vendor
        current_scope_ambiguous = (current_campaign_hit and not current_direct) or current_unscoped_link or current_unscoped_issue or selected_name_collision

    other_scope = bool(other_campaign_hits or other_link_campaigns or other_issue_campaigns)
    # A standalone client+vendor mention identifies only a vendor if the
    # catalog maps that vendor to exactly one campaign in this client's set.
    vendor_candidate_keys = set()
    target_vendor_candidate_keys = set()
    for name_key, keys in vendor_candidates.items():
        vendor_candidate_keys.update(keys)
        if not vendor_selection or name_key in target_vendor_names:
            target_vendor_candidate_keys.update(keys)
    vendor_maps_current = current["key"] in target_vendor_candidate_keys
    vendor_is_multi_campaign = len(target_vendor_candidate_keys) > 1
    vendor_only_current = (
        not current_direct and not current_campaign_hit and target_vendor_hit and
        client_hit and vendor_maps_current
    )

    # Preserve both sides of explicit mixed records instead of silently
    # allowing either the advertiser or a sibling campaign to dominate.
    current_evidence = current_direct or vendor_only_current
    current_reference = current_evidence or current_campaign_hit or current_scoped_link or current_scoped_issue
    if current_reference and other_scope:
        evidence.append("mixed:current+other")
        level = "ambiguous"
        reason = "同时出现当前归属证据与其他活动/供应商证据，需按原子消息拆分"
    elif current_scope_ambiguous:
        level = "ambiguous"
        reason = "活动证据无法确定所选供应商归属"
    elif current_direct:
        level = "direct"
        reason = "有当前活动名称或当前选择内的已知编号证据"
    elif vendor_only_current:
        if vendor_is_multi_campaign:
            level = "ambiguous"
            reason = "客户与供应商组合对应多个已知活动"
        else:
            level = "vendor"
            reason = "客户上下文与已知供应商一致，且目录中只对应当前活动"
    elif other_scope or other_current_vendor_link:
        level = "other"
        reason = "证据明确指向其他活动或未选择的供应商"
    elif client_hit and matched_vendor_names:
        # Vendor words that are not connected to the selected campaign are
        # not promoted just because the advertiser is also mentioned.
        if vendor_selection and not target_vendor_hit:
            level = "other"
            reason = "提到客户和其他供应商，但没有所选供应商证据"
        else:
            relevant = target_vendor_candidate_keys if vendor_selection else vendor_candidate_keys
            if current["key"] in relevant and len(relevant) > 1:
                level = "ambiguous"
                reason = "客户与供应商组合对应多个已知活动"
            elif current["key"] in relevant:
                level = "vendor"
                reason = "客户上下文与当前活动的已知供应商一致"
            else:
                level = "other"
                reason = "提到客户和已知供应商，但目录未将该组合关联到当前活动"
    elif client_hit:
        level = "client"
        reason = "只有客户或客户频道背景，没有确定的活动/供应商证据"
    elif matched_vendor_names:
        level = "other"
        reason = "只出现已知供应商，缺少当前客户或活动关联"
    elif identity_ambiguous and current_campaign_hit:
        level = "ambiguous"
        reason = "目录中存在多个同名活动，无法确定当前活动"
    else:
        level = "unrelated"
        reason = "没有命中当前客户、活动、供应商或已知编号"

    # If a current name is exact but a selected vendor cannot be resolved, it
    # is still useful evidence for a caller handling the record atomically.
    if current_campaign_hit and level == "ambiguous" and not campaign_names:
        campaign_names.append(current["name"])
    campaign_names = _unique(campaign_names)
    vendor_names = _unique(vendor_names + link_vendor_names)
    evidence = sorted(set(evidence))
    return {
        "level": level,
        "accepted": level in ("direct", "vendor"),
        "reason": reason,
        "evidence": evidence,
        "campaigns": campaign_names,
        "vendors": vendor_names,
    }
