"""Pi's API adapters. Online reads only; no mirror, collection export or attribution filter.
Credentials stay in the existing cloud secrets files. One call returns the source's response.
"""
import json
import re
import sys

from . import feeds, github

# Reviewed against the cloud server's tool descriptions, not a wildcard that enables future tools.
FEMINI_READ = """
get_campaign get_campaign_spend_view get_click_url get_click_url_spend_metrics get_client get_current_user
get_data_export get_email_thread get_fireflies_meeting get_hubspot_company get_hubspot_company_email
get_hubspot_company_note get_hubspot_contact get_hubspot_deal get_integration_endpoint get_ip_quality_monitor
get_mobile_app get_monthly_stat get_partner get_playbook get_playbook_item get_pod get_slack_channel
get_spend_request get_track_party get_tracked_file
list_campaign_mapping_proposals list_campaign_spend_views list_campaigns list_click_urls list_client_billings
list_client_budgets list_clients list_daily_campaign_metrics list_daily_campaign_spends list_daily_feeds
list_email_threads list_fireflies_meetings list_hubspot_companies list_hubspot_company_emails list_hubspot_company_notes
list_hubspot_contacts list_hubspot_deals list_integration_endpoints list_ip_quality_monitors list_mobile_apps
list_monthly_campaign_metrics list_monthly_spend_adjustments list_monthly_stats list_partner_billings
list_partner_categories list_partner_macros list_partner_payout_analyses list_partner_report_rows list_partners
list_partnership_comments list_playbooks list_pods list_proposed_changes list_shared_google_drive_files
list_slack_channels list_spend_requests list_supported_params list_track_parties list_track_party_report_rows
list_tracked_files list_users read_shared_google_drive_file search_google_drive_files search_playbook_items
get_skill list_skills read_skill_file preview_campaign_spend
""".split()


def _array(value):
    """MiniMax tool arguments can wrap an array in an XML-style item object.
    Only use at API fields whose documented type is array; never touch match.value."""
    while isinstance(value, dict) and set(value) == {"item"}:
        value = value["item"]
    return value if isinstance(value, list) else [value]


def _condition(value):
    if not isinstance(value, dict):
        return value
    value = dict(value)
    if any(k in value for k in ("must", "should", "must_not", "min_should")):
        return _filter(value)
    match = value.get("match")
    if isinstance(match, dict):
        match = dict(match)
        for k in ("any", "except"):
            if k in match:
                match[k] = _array(match[k])
        value["match"] = match
    if isinstance(value.get("range"), dict):
        unknown = set(value["range"]) - {"gt", "gte", "lt", "lte"}
        if unknown:
            raise ValueError("Qdrant range 不支持 " + ", ".join(sorted(unknown)) +
                             "；请使用 gt/gte/lt/lte。例如日期范围用 {\"gte\":\"2026-10-01T00:00:00Z\",\"lt\":\"2026-10-05T00:00:00Z\"}，不要用 from/to。")
    if isinstance(value.get("nested"), dict) and "filter" in value["nested"]:
        value["nested"] = {**value["nested"], "filter": _filter(value["nested"]["filter"])}
    if isinstance(value.get("has_id"), dict):
        value["has_id"] = _array(value["has_id"])
    return value


def _filter(value):
    if not isinstance(value, dict):
        return value
    # A single field condition is equivalent to one required condition.
    if "key" in value and any(k in value for k in ("match", "range", "geo_bounding_box", "geo_radius")):
        value = {"must": [value]}
    result = dict(value)
    for key in ("must", "should", "must_not"):
        if key in result and result[key] is not None:
            result[key] = [_condition(c) for c in _array(result[key])]
    if isinstance(result.get("min_should"), dict):
        part = dict(result["min_should"])
        if "conditions" in part:
            part["conditions"] = [_condition(c) for c in _array(part["conditions"])]
        if isinstance(part.get("min_count"), str) and part["min_count"].isdigit():
            part["min_count"] = int(part["min_count"])
        result["min_should"] = part
    return result


def request_body(body):
    """Transport-only coercion at typed Qdrant fields, not business filtering.
    Preserve identifiers and match values verbatim, including numeric strings."""
    if isinstance(body, str):
        body = json.loads(body)
    if not isinstance(body, dict):
        raise ValueError("body must be a Qdrant JSON object")
    body = dict(body)
    if "filter" in body:
        body["filter"] = _filter(body["filter"])
    payload = body.get("with_payload")
    if isinstance(payload, str) and payload in ("true", "false"):
        body["with_payload"] = payload == "true"
    elif isinstance(payload, dict):
        body["with_payload"] = (_array(payload) if set(payload) == {"item"} else
                                {k:_array(v) if k in ("include", "exclude") else v for k,v in payload.items()})
    if "ids" in body:
        body["ids"] = _array(body["ids"])
    return body


def feedai(args):
    op = args.get("operation")
    if op == "collections":
        data = feeds._qdrant("/collections")
        return data
    col = args.get("collection")
    if not isinstance(col,str) or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}",col):
        raise ValueError("Use a collection name returned by collections, not a path")
    path = "/collections/" + col
    if op == "info":
        return feeds._qdrant(path)
    if op not in ("scroll", "retrieve", "query"):
        raise ValueError("Only collections, info, scroll, retrieve and query are allowed")
    body = request_body(args.get("body") or {})
    body["with_vector"] = False
    body.setdefault("with_payload", True)
    if op in ("scroll", "query"):
        body["limit"] = int(body.get("limit", 50))  # 没传才默认 50；传多少给多少，上限由 FeedAI 自己决定
    if op == "retrieve" and not isinstance(body.get("ids"), list):
        raise ValueError("retrieve requires body.ids")
    suffix = {"scroll": "/points/scroll", "retrieve": "/points", "query": "/points/query"}[op]
    return feeds._qdrant(path + suffix, body)


def github_read(args):
    path = str(args.get("path") or "").lstrip("/")
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", path) or ".." in path.split("/"):
        raise ValueError("Use a GitHub API path, not a URL")
    if not (re.fullmatch(r"search/(issues|code)", path) or re.fullmatch(r"repos/feed-mob/[\w.-]+(?:/[\w./-]+)?", path)):
        raise ValueError("Only read requests for feed-mob repositories and search are allowed")
    params = dict(args.get("params") or {})
    params["per_page"] = int(params.get("per_page", 50))  # 没传才默认 50；上限由 GitHub 自己决定（100）
    if path.startswith("search/"):
        params["q"] = str(params.get("q") or "") + " org:feed-mob"
    return github.GH().get(path, params=params, paginate=False)


def fireflies(args):
    op = args.get("operation")
    if op == "list":
        variables = {k: v for k, v in (args.get("variables") or {}).items() if k in ("fromDate", "toDate", "limit", "skip")}
        variables["limit"] = int(variables.get("limit", 50))  # 没传才默认 50；上限由 Fireflies 自己决定
        if "skip" in variables:  # 模型常把数字写成字符串（"0"）；Fireflies 只收整数
            variables["skip"] = max(0, int(variables["skip"]))
        return feeds._gql(feeds.LIST_QUERY, variables)
    if op == "detail":
        ident = str(args.get("id") or "")
        if not ident or len(ident) > 200:
            raise ValueError("detail requires id from a list result")
        return feeds._gql(feeds.DETAIL_QUERY, {"transcriptId": ident})
    raise ValueError("Only list and detail are allowed; no GraphQL mutations")


def dispatch(name, args):
    fn = {"feedai_api": feedai, "github_api": github_read, "fireflies_api": fireflies}.get(name)
    if fn is None:
        raise ValueError("Unknown API tool")
    return fn(args)


def main():
    # Tool arguments only (no credentials) are passed as one JSON argv item; never a shell command.
    try:
        req = json.loads(sys.argv[1])
        result = {"ok": True, "data": dispatch(req["tool"], req.get("args") or {})}
    except Exception as exc:
        message = str(exc)
        for file in ("feeds.env", "github.env", "femini.env"):
            for key, value in feeds._env_file(file).items():
                if ("KEY" in key or "TOKEN" in key) and value:
                    message = message.replace(value, "[redacted]")
        result = {"ok": False, "error": type(exc).__name__ + ": " + message}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
