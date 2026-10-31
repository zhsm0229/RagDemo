#!/usr/bin/env python3
"""NFR 性能取证脚本（商用就绪工单 15）：在目标部署机产出可归档的性能基线数字。

口径对齐 docs/specs/test-cases.md TC-NFR：
- NFR-01 首 token：连续 N 次问答 SSE 流，记请求发出→首个 token 事件耗时，P50/P95
- NFR-02 检索：检索测试接口（admin）连续 N 次，P50/P95（含 embed+召回+重排全链）
- NFR-03 稳态：20 并发 × 2 分钟持续问答，成功率 + P95 首 token（并发不降级判据）

用法（部署机）：
    python3 deploy/nfr-check.py                 # 全部三项，输出 nfr-results/<ts>.json
    python3 deploy/nfr-check.py --quick         # 只跑 01/02 小样本冒烟
环境变量：NFR_BASE_URL（缺省 http://localhost:8000）、NFR_USER/NFR_PASS（必填，
    显式口令不落字面量——工单 04 同口径）、NFR_CONCURRENCY（缺省 20）、
    NFR_DURATION_S（缺省 120）。
"""

import argparse
import json
import statistics
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

_BASE = "http://localhost:8000"
_QUESTION = "公司年假有多少天"  # 语义无关紧要：取证的是链路延迟分布而非答案质量


def _login(client: httpx.Client, base: str, user: str, password: str) -> None:
    resp = client.post(f"{base}/api/auth/login", json={"username": user, "password": password})
    if resp.status_code != 200 or resp.json().get("code") != 0:
        raise SystemExit(f"登录失败：{resp.status_code} {resp.text[:120]}（检查 NFR_USER/NFR_PASS）")
    # Cookie 由 client 内部 cookie jar 持有，后续请求自动携带


def _sse_first_token_ms(client: httpx.Client, base: str, question: str) -> float:
    """一次问答流：请求发出 → 首个 token 事件的毫秒数（含检索/重排全链）。"""
    start = time.perf_counter()
    with client.stream(
        "POST", f"{base}/api/chat/stream", json={"question": question}, timeout=180
    ) as resp:
        if resp.status_code != 200:
            raise RuntimeError(f"stream {resp.status_code}")
        for line in resp.iter_lines():
            if line.startswith("event: token"):
                return (time.perf_counter() - start) * 1000
    raise RuntimeError("流结束未见 token 事件")


def _pctl(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))
    return round(ordered[idx], 1)


def _run_01_first_token(client: httpx.Client, base: str, rounds: int) -> dict:
    values = [_sse_first_token_ms(client, base, _QUESTION) for _ in range(rounds)]
    return {
        "rounds": rounds,
        "p50_ms": _pctl(values, 50),
        "p95_ms": _pctl(values, 95),
        "max_ms": _pctl(values, 100),
    }


def _run_02_retrieval(client: httpx.Client, base: str, rounds: int) -> dict:
    values: list[float] = []
    for _ in range(rounds):
        start = time.perf_counter()
        resp = client.post(
            f"{base}/api/admin/retrieval-test",
            json={"question": _QUESTION, "deptCode": "all"},
            timeout=60,
        )
        elapsed = (time.perf_counter() - start) * 1000
        if resp.status_code == 200 and resp.json().get("code") == 0:
            values.append(elapsed)
    return {
        "rounds": len(values),
        "p50_ms": _pctl(values, 50),
        "p95_ms": _pctl(values, 95),
    }


def _run_03_sustained(
    client_factory, base: str, user: str, password: str, concurrency: int, duration_s: int
) -> dict:
    """并发稳态：N 线程各自登录后持续问答，记成功/失败与首 token 分布。"""
    results: list[float] = []
    failures: list[str] = []
    lock = threading.Lock()
    deadline = time.monotonic() + duration_s
    stop = threading.Event()

    def _worker() -> None:
        client = httpx.Client(base_url=base, timeout=180)
        try:
            _login(client, base, user, password)
        except SystemExit as exc:
            with lock:
                failures.append(str(exc))
            return
        while not stop.is_set() and time.monotonic() < deadline:
            try:
                results.append(_sse_first_token_ms(client, base, _QUESTION))
            except Exception as exc:  # noqa: BLE001 — 取证脚本：失败计数不中断采样
                with lock:
                    failures.append(str(exc)[:80])
        client.close()

    threads = [threading.Thread(target=_worker) for _ in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    total = len(results) + len(failures)
    return {
        "concurrency": concurrency,
        "duration_s": duration_s,
        "requests": total,
        "success": len(results),
        "failure": len(failures),
        "success_rate": round(len(results) / total, 4) if total else 0.0,
        "p95_first_token_ms": _pctl(results, 95),
    }


def main() -> None:
    import os

    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="只跑 01/02 小样本冒烟")
    args = parser.parse_args()

    base = os.environ.get("NFR_BASE_URL", _BASE)
    user = os.environ.get("NFR_USER", "")
    password = os.environ.get("NFR_PASS", "")
    if not user or not password:
        raise SystemExit("必须显式设置 NFR_USER / NFR_PASS（口令不落字面量，工单 04 口径）")

    report: dict = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "baseUrl": base,
        "nfr01": None,
        "nfr02": None,
        "nfr03": None,
    }
    with httpx.Client(base_url=base, timeout=180) as client:
        _login(client, base, user, password)
        rounds = 10 if args.quick else 30
        print(f"[nfr] NFR-01 首 token：{rounds} 轮……")
        report["nfr01"] = _run_01_first_token(client, base, rounds)
        print(f"[nfr]   {report['nfr01']}")
        print(f"[nfr] NFR-02 检索：{rounds} 轮……")
        report["nfr02"] = _run_02_retrieval(client, base, rounds)
        print(f"[nfr]   {report['nfr02']}")
        if not args.quick:
            print("[nfr] NFR-03 并发稳态……")
            report["nfr03"] = _run_03_sustained(
                lambda: httpx.Client(base_url=base, timeout=180),
                base,
                user,
                password,
                int(os.environ.get("NFR_CONCURRENCY", "20")),
                int(os.environ.get("NFR_DURATION_S", "120")),
            )
            print(f"[nfr]   {report['nfr03']}")

    out_dir = Path("nfr-results")
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"nfr-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"[nfr] 已归档：{out}")


if __name__ == "__main__":
    main()
