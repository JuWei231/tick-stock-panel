"""通达信数据源插件。"""
from app.plugins.tdx.provider import TdxProvider

PROVIDER_NAME = "tdx"

__all__ = ["PROVIDER_NAME", "TdxProvider"]
