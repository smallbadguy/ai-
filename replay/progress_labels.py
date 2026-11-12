"""业务版「进行中事项」页签用到的文字（中英文）和取字小工具。"""

T = {
    "zh": dict(
        title="进行中事项", sub="还没办完的事，以及下一步谁负责",
        status=dict(in_progress="进行中", waiting_on_others="等待对方", on_hold="暂缓"),
        scope=dict(this_campaign="本活动", same_client="同客户其他", client_general="客户层面", unclear="范围待定"),
        stale_days="{n} 天没有动静", next="下一步", owner="负责人", unconfirmed="待确认",
        basis=dict(self_commitment="自己承诺", direct_request="被点名请求", explicit_assignment="明确指派", none="暂无明确依据"),
        last="最近动静", evidence="原话依据", empty="目前没有进行中的事项",
        foot="内容由 AI 整理；引用的每一句原话都已与原始记录逐字核对，没有依据的负责人写「待确认」。"),
    "en": dict(
        title="Work in progress", sub="What is still open, and who owns the next step",
        status=dict(in_progress="In progress", waiting_on_others="Waiting on others", on_hold="On hold"),
        scope=dict(this_campaign="This campaign", same_client="Same client, other", client_general="Client-level", unclear="Scope unclear"),
        stale_days="No activity for {n} days", next="Next step", owner="Owner", unconfirmed="To be confirmed",
        basis=dict(self_commitment="Said they would", direct_request="Asked by name", explicit_assignment="Explicitly assigned", none="No clear basis yet"),
        last="Last activity", evidence="Quotes", empty="Nothing in progress right now",
        foot="Compiled by AI. Every quote is checked word for word against the original records; owners without a clear basis are shown as “To be confirmed”."),
}


def pick(it, key, lang):
    """英文页优先用 *_en，没有就退回中文。"""
    return (it.get(key + "_en") if lang == "en" else it.get(key)) or it.get(key) or ""
