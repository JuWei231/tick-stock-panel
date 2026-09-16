"""Ticker 代码归一化 — 由 SKILL.md「市场前缀规则 / Ticker 格式归一化」移植。

⚠️ 拿到用户输入的代码，**先过一遍 ``norm_ticker()`` 再传给任何端点**，最省事也最安全。
不匹配时抛 ``ValueError``，绝不静默返回空串或猜一个代码——否则调用方会把
「代码格式写错」误读成「这只票没有数据」，或更糟：拿到另一只股票的数据还以为是对的。
"""
from __future__ import annotations

import re

#: 沪市指数白名单：与深市 000xxx 个股同段，需白名单区分
#: （沪深300/上证50/中证500/科创50/中证1000/上证180）
SH_INDEX = {"000300", "000905", "000016", "000688", "000852", "000010"}

# 整串锚定匹配，只认下表列出的写法；市场标识前缀、后缀**二选一，不能同时出现**。
# ⚠️ 两个坑都会造成「静默拿到另一只股票的数据」，比报错危险得多：
#   ① 别用 re.search(r"\d{6}") 从任意串里"捞"6 位："6005190"/"foo600519bar" 会被截成 600519。
#   ② 别让前后缀同时可选：`SH000001.SZ` 这种自相矛盾的写法会被照单全收，
#      而 000001 恰是歧义码（sh000001=上证指数 / sz000001=平安银行），静默丢掉市场信息＝选错标的。
# 捕获组：1=前缀市场 2=前缀式代码 | 3=后缀式代码 4=后缀市场
# 市场标识要**同时**从前缀和后缀取——只认 startswith("sh") 会漏掉 `000001.SH` 这种后缀写法。
_TICKER_RE = re.compile(r"^(?:(sh|sz|bj)(\d{6})|(\d{6})(?:\.(sh|sz|bj))?)$", re.IGNORECASE)


def get_prefix(code: str) -> str:
    """6 位代码 → 市场前缀（sh/sz/bj）。支持显式前缀/后缀透传以解决歧义。

    - ``000001`` 默认按个股 → ``sz``（平安银行）；要上证指数请显式传 ``sh000001``。
    - ``000016`` 默认按沪指数 → ``sh``（上证50）；要深康佳A 请传 ``sz000016``。
    """
    c = code.lower().strip()
    if c.endswith((".sh", ".sz", ".bj")):  # 后缀写法与前缀等价：000016.SH ≡ sh000016
        return c[-2:]
    if c.startswith(("sh", "sz", "bj")):  # 显式前缀透传
        return c[:2]
    if c.startswith("92"):  # 北交所 2024-10 起的新股号段，必须先于下面的 9x 判断
        return "bj"
    if c.startswith(("5", "6", "9")):  # 5x=沪 ETF/LOF，6/9=沪个股（900xxx=沪 B 股）
        return "sh"
    if c.startswith(("4", "8")):  # 4x/8x=北交所【老号段，多数已迁 920】
        return "bj"
    if c in SH_INDEX:  # 沪深300/上证50 等沪指数（000xxx）
        return "sh"
    return "sz"  # 深市个股/ETF（00/30/15x/16x/159 等），深指数 399xxx 亦走 sz


def _natural_market(digits: str) -> str:
    """6 位码的自然归属市场。仅用于校验显式前缀是否自相矛盾。

    注意 000xxx 是沪指数/深个股共用的歧义段，由调用处单独处理，不走这里。
    """
    if digits.startswith(("4", "8", "92")):
        return "bj"  # 北交所：与 get_prefix() 同一套号段规则（92x 现行 / 4x·8x 老号段）
    if digits[0] in ("5", "6", "9"):
        return "sh"  # 5x 沪 ETF/LOF，6xx 沪个股，9xx 沪 B 股
    return "sz"  # 00x/30x/15x/16x/39x 等


def norm_ticker(code: str, stock_only: bool = False) -> str:
    """任意受支持写法 → 纯 6 位数字代码。

    支持 600519 / SH600519 / sh600519 / 600519.SH / BJ920982 等。
    stock_only=True：个股专用接口（研报、一致预期等）传这个，会拒绝显式指数写法。
    ⚠️ 不匹配时**抛 ValueError**，绝不静默返回空串或猜一个代码。
    """
    raw = str(code).strip()
    m = _TICKER_RE.match(raw)
    if not m:
        raise ValueError(
            f"无法把 {code!r} 解析为 6 位股票代码；"
            f"支持格式：600519 / SH600519 / sh600519 / 600519.SH"
            f"（前缀与后缀二选一，不能同时写）"
        )
    digits = m.group(2) or m.group(3)
    market = (m.group(1) or m.group(4) or "").lower()  # 前缀式与后缀式都要认
    # 归一化会丢掉市场标识，若标识与号段矛盾就会静默落到另一只票上，必须在这里拦。
    if market:
        if digits.startswith("000"):
            # 000xxx 是**沪市指数 / 深市个股共用**的歧义段，显式标识在这里是「消歧」不是「矛盾」。
            if market == "bj":
                raise ValueError(f"{code!r} 市场标识与号段矛盾：000xxx 不属北交所。")
            # 沪市个股只有 600/601/603/605/688/689（B 股 900），不存在 000xxx 沪市个股，
            # 所以「显式 sh + 000 段」必然是指数。
            if stock_only and market == "sh":
                raise ValueError(
                    f"{code!r} 指向沪市指数而非个股（沪市无 000xxx 个股），本接口只服务个股。"
                    f"要查同号段的深市个股请显式传 sz{digits}。"
                )
        else:
            nat = _natural_market(digits)
            if market != nat:
                raise ValueError(
                    f"{code!r} 的市场标识与号段矛盾：{digits} 属 {nat} 市，而不是 {market} 市。"
                    f"（改用 {nat}{digits} 或去掉市场标识）"
                )
    return digits


def em_market_code(code: str) -> int:
    """东财 secid 的市场号：**沪=1，深/北=0**。

    ⚠️ 绝不要用 ``code.startswith("6")`` 判市场——那会把沪市 ETF（51x）、科创板 ETF（588x）、
    沪 B 股（900x）全部错判成深市，接口返回 ``data: null``。
    """
    return 1 if get_prefix(code) == "sh" else 0


def em_secid(code: str) -> str:
    """东财 push2/push2his 的 secid，如 ``1.600519`` / ``0.300750``。"""
    return f"{em_market_code(code)}.{norm_ticker(code)}"
