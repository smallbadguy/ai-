"""Vendor selection helpers for the replay service.

The generated replay pages are served as they are: the service only adds the
existing navigation bar.  2026-11-02: the extra status bar under the page header
was removed again; the replay page's own design must not change (user request).
"""
import contextlib
import inspect
import json
import re
from urllib.parse import urlencode

from replay import campaign, pipeline
from replay.util import out_dir


VENDOR_KEY = re.compile(r"(?:[0-9]+|v-[0-9a-f]{12})\Z")
COVERAGE = {"zh": "渠道清单包含当前和已知已暂停的渠道，历史清单可能不完整。",
            "en": "Channel lists include current and known paused channels; history may be incomplete."}


def option_label(item, lang):
    """下拉框里的渠道：名称 + 它的 click URL 号，跟首页活动名下面的号对得上；暂停、状态未知写在后面。"""
    z = lang == "zh"
    links = " ".join("#" + str(x) for x in item.get("links") or [])
    text = str(item.get("name") or item.get("id") or "") + (" " + links if links else "")
    if item.get("active") is False:
        text += "（已暂停）" if z else " (Paused)"
    elif item.get("active") is None:
        text += "（状态待确认）" if z else " (Status pending)"
    return text


class InvalidVendor(ValueError):
    """The requested vendor key is malformed."""


class UnknownVendor(LookupError):
    """The vendor is not associated with the requested campaign."""


def _scope_module():
    try:
        from replay import scope
    except ImportError:
        return None
    return scope


@contextlib.contextmanager
def use_scope(cid, vendor=None):
    """Enter a vendor scope when the replay foundation is available."""
    module = _scope_module()
    if module is None:
        if vendor:
            raise RuntimeError("Vendor replay support is not available")
        yield
        return
    with module.use(cid, vendor):
        yield


def normalize_vendor(value):
    """Return None for the all-vendors scope, otherwise a safe stable key."""
    if value is None or value == "" or value == "all":
        return None
    module = _scope_module()
    try:
        result = module.valid_vendor(value) if module else value
    except (TypeError, ValueError) as exc:
        raise InvalidVendor(str(exc)) from None
    if result in (None, "", "all"):
        return None
    result = str(result)
    if not VENDOR_KEY.fullmatch(result):
        raise InvalidVendor("invalid vendor key")
    return result


def validate_vendor(cid, value):
    """Normalize and verify a vendor against this campaign; never fall back."""
    vendor = normalize_vendor(value)
    if vendor is None:
        return None
    check = getattr(campaign, "validate_vendor", None)
    if check is None:
        raise UnknownVendor("vendor registry is unavailable")
    try:
        valid = check(cid, vendor)
    except (KeyError, LookupError) as exc:
        raise UnknownVendor(str(exc)) from None
    except ValueError as exc:
        # A key that passed the syntax check but is absent from this campaign.
        raise UnknownVendor(str(exc)) from None
    if valid is False:
        raise UnknownVendor("vendor does not belong to this campaign")
    return vendor


def options(cid, refresh=False):
    """Return sanitized, stable vendor options without discarding paused ones."""
    getter = getattr(campaign, "vendor_options", None)
    if getter is None:
        return []
    found = getter(cid, refresh=refresh) or []
    clean = []
    seen = set()
    for item in found:
        if not isinstance(item, dict):
            continue
        try:
            key = normalize_vendor(item.get("id"))
        except InvalidVendor:
            continue
        if key is None or key in seen:
            continue
        seen.add(key)
        clean.append({
            "id": key,
            "name": str(item.get("name") or key),
            "links": [str(link) for link in (item.get("links") or [])],
            "active": item.get("active") if isinstance(item.get("active"), bool) else None,
        })
    return clean


def refresh_pipeline(cid, vendor=None, *, reserved_lock=None):
    params = inspect.signature(pipeline.refresh).parameters
    if "_reserved_lock" in params and reserved_lock is not None:
        return pipeline.refresh(cid, vendor=vendor, _reserved_lock=reserved_lock)
    if "vendor" in params or any(p.kind == p.VAR_KEYWORD for p in params.values()):
        return pipeline.refresh(cid, vendor=vendor)
    if vendor is not None:
        raise RuntimeError("Vendor-scoped refresh support is not available")
    return pipeline.refresh(cid)


def _read_state(cid, vendor):
    if vendor is None:  # the whole-campaign scope keeps the original call shape
        return pipeline.read_state(cid) or {}
    params = inspect.signature(pipeline.read_state).parameters
    if "vendor" in params or any(p.kind == p.VAR_KEYWORD for p in params.values()):
        return pipeline.read_state(cid, vendor=vendor) or {}
    if vendor is not None:
        raise RuntimeError("Vendor-scoped state support is not available")
    return pipeline.read_state(cid) or {}


def scope_status(cid, vendor=None, running=None, root=None):
    """Describe the pages and state of one isolated campaign/vendor scope."""
    with use_scope(cid, vendor):
        state = _read_state(cid, vendor)
        root = root if root is not None else out_dir(cid)
        phase = running(cid, vendor, state) if running else None
        return {
            "business": (root / "business.en.html").is_file(),
            "detail": (root / "replay.en.html").is_file(),
            "running": phase == "running",
            "phase": phase,
            "last_ok": state.get("last_ok"),
            "state": state,
        }


def all_scope_statuses(cid, vendor_options, running=None):
    """Return all-channel and per-vendor status records keyed by stable ID."""
    scopes = {"all": scope_status(cid, None, running)}
    for item in vendor_options:
        key = item["id"]
        scopes[key] = scope_status(cid, key, running)
    return scopes


def scope_query(vendor):
    return "?" + urlencode({"vendor": vendor}) if vendor else ""


def safe_json(data):
    return (json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))
