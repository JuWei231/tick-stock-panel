"""候选 Key 先探后存 — 独立子进程实探。

原生 DLL 每进程加载一次且绑定一个 token: 主进程可能已用旧 Key 加载,
候选 Key 无法在进程内替换, 因此探 Key 必须另起进程(临时 lib 目录 +
TDX_AI_DATA_LIB 指向), 结论以 JSON 回传, 失败不落盘。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_PROBE_ENV_KEY = "TDX_PROBE_CANDIDATE_KEY"

# 子进程入口: 复用 client.write_token_ini/materialize_lib 保持与主进程同一套写入逻辑
_SCRIPT = """
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

key = os.environ.pop("TDX_PROBE_CANDIDATE_KEY", "")
tmp = Path(tempfile.mkdtemp(prefix="tdx_probe_"))
try:
    from app.plugins.tdxquant import client as tc

    dll = tc.materialize_lib(key, tmp)
    os.environ[tc.LIB_ENV] = str(dll)
    from tdxaidata import tqs
    from tdxaidata.tdxaidata import _NativeTdxAiData

    # 快照端点不校验 token(实测坏 Key 也能返回), 鉴权判定必须用日K;
    # 快照轮询仅作连接预热(DLL 冷启动有未就绪窗口)。
    for _ in range(5):
        try:
            if tqs.get_market_snapshot_batch(stock_list=["600519.SH"], field_list=["Now"]):
                break
        except Exception:
            pass
        time.sleep(1.2)
    ok = False
    reason = ""
    for _attempt in range(3):
        try:
            raw = _NativeTdxAiData.get_instance().get_his_dats(
                "600519.SH", "", "2026-01-01 00:00:00", "1d", "none", 3
            )
            v = tqs._clean(raw)
            if isinstance(v, dict) and v.get("Date"):
                ok = True
                break
            reason = "日K无数据(Key 无效或无权限)"
        except Exception as exc:
            reason = str(exc)[:200]
        time.sleep(1.2)
    print(json.dumps({"ok": ok, "reason": reason}))
except Exception as exc:
    print(json.dumps({"ok": False, "reason": str(exc)[:300]}))
finally:
    shutil.rmtree(tmp, ignore_errors=True)
"""


def run(api_key: str, timeout_s: float = 40.0) -> tuple[bool, str]:
    """子进程实探候选 Key(快照一次)。DLL 首连重试在 SDK 层不可控, 超时给足。"""
    if not api_key:
        return False, "Key 为空"
    backend_root = Path(__file__).resolve().parents[3]
    env = {**os.environ, _PROBE_ENV_KEY: api_key, "PYTHONPATH": str(backend_root)}
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _SCRIPT],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=str(backend_root),
            env=env,
        )
    except subprocess.TimeoutExpired:
        return False, "探测超时(网络不可达或 Key 无效)"
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                break
            if payload.get("ok"):
                return True, "ok"
            return False, f"Key 无效或网络失败: {payload.get('reason', '未知原因')}"
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return False, f"探测失败: {detail[-1][:200] if detail else '无输出'}"
