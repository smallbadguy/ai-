"""A replay variant is (campaign, optional backend vendor), never a process-global setting.

The source cache stays shared. Material, AI caches, output pages and status are
separate for each variant; all variants of a campaign still share its write lock.
"""
import contextlib
import contextvars
import functools
import re

_SELECTION = contextvars.ContextVar("replay_selection", default=None)
_VENDOR = re.compile(r"(?:[1-9][0-9]{0,11}|v-[0-9a-f]{12})\Z")


def valid_vendor(value):
    if value is None or value in ("", "all"):
        return None
    value = str(value)
    if not _VENDOR.fullmatch(value):
        raise ValueError("Invalid vendor identifier")
    return value


def vendor_for(cid):
    selected = _SELECTION.get()
    return selected[1] if selected and selected[0] == str(cid) else None


@contextlib.contextmanager
def use(cid, vendor=None):
    from .util import valid_number
    selected = (valid_number(cid), valid_vendor(vendor))
    token = _SELECTION.set(selected)
    try:
        yield selected[1]
    finally:
        _SELECTION.reset(token)


def path(base, cid, vendor=None):
    """base is the existing aggregate directory (or a per-campaign state directory)."""
    key = valid_vendor(vendor) if vendor is not None else vendor_for(cid)
    return base / "vendors" / key if key else base


def bind(fn):
    """Capture one variant for a callback; each invocation gets its own context copy."""
    ctx = contextvars.copy_context()

    @functools.wraps(fn)
    def run(*args, **kwargs):
        return ctx.copy().run(fn, *args, **kwargs)
    return run


def thread_map(pool, fn, iterable):
    return pool.map(bind(fn), iterable)


def meta(cid):
    return {"campaign_id": str(cid), "vendor_id": vendor_for(cid)}
