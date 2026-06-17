#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
涨停复盘底稿生成脚本

收盘后拉取涨停池、市场情绪、板块榜数据，整理成一份 Markdown 复盘底稿，
方便直接复制到博客里再加工。数据源通过项目的 DataFetcherManager 获取，
享受多数据源自动 fallback 能力（akshare -> efinance -> tushare -> ...）。

输出内容：
    1. 市场情绪仪表盘（涨停/跌停/炸板率/成交额 等）
    2. 涨停梯队（按连板数从高到低分组，全量涨停池）
    3. 题材分布（按所属行业聚合涨停家数）
    4. 主线板块榜（行业涨跌幅 Top/Bottom）
    5. 今日之最（最高封板资金 / 最高连板 / 最高换手）

使用方法：
    python scripts/limit_up_review.py                  # 复盘当天
    python scripts/limit_up_review.py --date 20260609  # 复盘指定日期
    python scripts/limit_up_review.py --out review.md  # 同时写入文件
    python scripts/limit_up_review.py --send           # 生成后推送到报告通知渠道

说明：
    - 涨停池、炸板池数据为收盘后口径，盘中运行结果会不准。
    - 默认拉取当日全量涨停池，不做精简截断。
    - 涨停家数直接从涨停池明细中统计，与梯队明细同源。
    - 炸板率 = 炸板数 / (涨停家数 + 炸板数)，分母与涨停池口径一致。
    - 跌停池、炸板池直接调用 akshare 接口（含重试 + 多版本接口名兼容），
      因为项目 DataFetcherManager 不暴露这两个接口。
"""

import argparse
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# 添加项目根目录到路径，复用项目内的数据源适配
sys.path.insert(0, str(Path(__file__).parent.parent))

from data_provider.base import DataFetcherManager


def _fmt_amount(value: Optional[float]) -> str:
    """把元/万元口径的金额转成更易读的‘亿’，None 显示 -。"""
    if value is None:
        return "-"
    try:
        return f"{float(value) / 1e8:.2f} 亿"
    except (TypeError, ValueError):
        return "-"


def _fmt_seal(value: Optional[float]) -> str:
    """封板资金（元）转‘亿’。"""
    if value is None:
        return "-"
    try:
        return f"{float(value) / 1e8:.2f} 亿"
    except (TypeError, ValueError):
        return "-"


def apply_negative_board_counts(
    stats: Dict[str, Any],
    counts: Optional[Dict[str, int]],
) -> Optional[int]:
    """用东财跌停池/炸板池覆盖统计值，并返回炸板数。"""
    if not counts:
        return None

    if counts.get("limit_down_count") is not None:
        stats["limit_down_count"] = counts["limit_down_count"]
    return counts.get("break_count")


def _fetch_pool_count_with_retry(
    ak: Any,
    api_names: List[str],
    date: str,
    label: str,
    retries: int = 3,
    sleep_seconds: int = 20,
) -> Optional[int]:
    """调用东财涨跌停池接口，失败时重试；兼容不同 Akshare 版本的接口名。"""
    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        for api_name in api_names:
            api_func = getattr(ak, api_name, None)
            if api_func is None:
                continue
            try:
                df = api_func(date=date)
                return 0 if df is None or df.empty else int(len(df))
            except Exception as exc:
                last_error = exc
                print(
                    f"[警告] {label}接口 {api_name} 第 {attempt}/{retries} 次失败: {exc}",
                    file=sys.stderr,
                )
        if attempt < retries:
            time.sleep(sleep_seconds)

    if last_error is not None:
        print(f"[警告] {label}获取失败，已放弃: {last_error}", file=sys.stderr)
    return None


def fetch_negative_board_counts(date: str) -> Optional[Dict[str, int]]:
    """获取东财跌停池和炸板池统计，返回 {limit_down_count, break_count}。

    涨停家数不在此处获取，而是直接从涨停池明细中统计（避免重复调 API）。
    """
    try:
        import akshare as ak
    except Exception as exc:
        print(f"[警告] akshare 导入失败，无法获取跌停/炸板池统计: {exc}", file=sys.stderr)
        return None

    # 只拉跌停池和炸板池，涨停家数从涨停池明细统计
    api_groups = {
        "limit_down_count": (
            "跌停池",
            [
                "stock_zt_pool_dtgc_em",
                "stock_zt_pool_dt_em",
                "stock_em_zt_pool_dtgc",
            ],
        ),
        "break_count": ("炸板池", ["stock_zt_pool_zbgc_em"]),
    }

    counts: Dict[str, int] = {}
    for field_name, (label, api_names) in api_groups.items():
        count = _fetch_pool_count_with_retry(ak, api_names, date, label)
        if count is not None:
            counts[field_name] = count

    return counts or None


# 全量拉取时传给 get_limit_up_pool 的上限（正常交易日涨停家数远低于此值）
_FULL_POOL_FETCH_LIMIT = 10000


def fetch_limit_up_pool(
    manager: DataFetcherManager,
    date: str,
) -> Optional[List[Dict[str, Any]]]:
    """拉取当日全量涨停池，享受 DataFetcherManager 的多源 fallback。"""
    for attempt in range(1, 4):
        pool = manager.get_limit_up_pool(date=date, n=_FULL_POOL_FETCH_LIMIT)
        if pool:
            return pool
        if attempt < 3:
            print(f"[警告] 涨停池明细第 {attempt}/3 次为空，20 秒后重试...", file=sys.stderr)
            time.sleep(20)
    return None


def build_sentiment_section(
    stats: Optional[Dict[str, Any]],
    break_count: Optional[int],
) -> str:
    """市场情绪仪表盘。"""
    lines = ["## 一、市场情绪仪表盘", ""]
    data = stats or {}
    has_market_breadth = any(
        data.get(key) not in (None, 0, 0.0)
        for key in ("up_count", "down_count", "flat_count", "total_amount")
    )
    has_limit_pool = data.get("limit_up_count") is not None or data.get("limit_down_count") is not None
    if not has_market_breadth and not has_limit_pool:
        lines.append("> 市场涨跌统计获取失败（可能是非交易日或数据源异常）。")
        lines.append("")
        return "\n".join(lines)

    up = data.get("up_count", 0)
    down = data.get("down_count", 0)
    flat = data.get("flat_count", 0)
    limit_up = data.get("limit_up_count", 0)
    limit_down = data.get("limit_down_count", 0)
    total_amount = data.get("total_amount", 0.0)

    if break_count is None:
        break_rate = "N/A"
        break_display = "N/A"
    else:
        # 炸板率分母用全市场涨停家数，不用涨停池截取条数
        denom = limit_up + break_count
        break_rate = f"{(break_count / denom * 100):.1f}%" if denom > 0 else "0.0%"
        break_display = str(break_count)

    lines.extend([
        "| 指标 | 数值 |",
        "| --- | --- |",
        f"| 涨停家数（封住） | {limit_up} |",
        f"| 跌停家数 | {limit_down} |",
        f"| 上涨家数 | {up} |",
        f"| 下跌家数 | {down} |",
        f"| 平盘家数 | {flat} |",
        f"| 炸板数 | {break_display} |",
        f"| 炸板率 | {break_rate} |",
        f"| 两市成交额 | {total_amount:.0f} 亿 |",
        "",
        "> 涨跌停家数取自东财涨跌停池；上涨/下跌/成交额仍来自全市场行情统计。",
        "> 炸板率 = 炸板数 / (涨停家数 + 炸板数)。越高说明封板意愿越弱，次日竞价需谨慎。",
        "",
    ])
    return "\n".join(lines)


def build_ladder_section(
    pool: Optional[List[Dict[str, Any]]],
) -> str:
    """涨停梯队，按连板数从高到低分组。"""
    lines = ["## 二、涨停梯队（按连板数）", ""]
    if not pool:
        lines.append("> 涨停池为空或获取失败。")
        lines.append("")
        return "\n".join(lines)

    lines.append(f"> 以下共 **{len(pool)}** 只（全量涨停池）。")
    lines.append("")

    # 按连板数分组
    tiers: Dict[int, List[Dict[str, Any]]] = {}
    for item in pool:
        boards = item.get("consecutive_boards") or 1
        tiers.setdefault(boards, []).append(item)

    for boards in sorted(tiers.keys(), reverse=True):
        stocks = tiers[boards]
        tier_name = f"{boards} 连板" if boards > 1 else "首板"
        lines.append(f"### {tier_name}（{len(stocks)} 只）")
        lines.append("")
        lines.append("| 名称 | 代码 | 行业 | 首封时间 | 封板资金 | 炸板次数 | 换手率 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for s in stocks:
            turnover = s.get("turnover_rate")
            turnover_str = f"{turnover:.1f}%" if turnover is not None else "-"
            lines.append(
                f"| {s.get('name', '-')} | {s.get('code', '-')} | "
                f"{s.get('industry') or '-'} | {s.get('first_limit_time') or '-'} | "
                f"{_fmt_seal(s.get('seal_amount'))} | {s.get('break_count', 0)} | "
                f"{turnover_str} |"
            )
        lines.append("")
    return "\n".join(lines)


def build_theme_section(pool: Optional[List[Dict[str, Any]]], top: int = 10) -> str:
    """题材分布，按所属行业聚合涨停家数。"""
    lines = ["## 三、题材分布（涨停归属）", ""]
    if not pool:
        lines.append("> 涨停池为空，无法聚合题材。")
        lines.append("")
        return "\n".join(lines)

    counter: Counter = Counter()
    for item in pool:
        industry = item.get("industry") or "未分类"
        counter[industry] += 1

    lines.append("| 题材/行业 | 涨停家数 |")
    lines.append("| --- | --- |")
    for industry, count in counter.most_common(top):
        lines.append(f"| {industry} | {count} |")
    lines.append("")
    lines.append("> 涨停家数最多、且含最高连板龙头的题材，通常即为当日主线。")
    lines.append("")
    return "\n".join(lines)


def build_sector_section(rankings: Optional[Any]) -> str:
    """主线板块榜。"""
    lines = ["## 四、行业板块涨跌榜", ""]
    if not rankings:
        lines.append("> 板块涨跌榜获取失败。")
        lines.append("")
        return "\n".join(lines)

    top, bottom = rankings
    lines.append("**领涨板块**")
    lines.append("")
    lines.append("| 板块 | 涨跌幅 |")
    lines.append("| --- | --- |")
    for s in top:
        lines.append(f"| {s.get('name', '-')} | {s.get('change_pct', 0):+.2f}% |")
    lines.append("")
    lines.append("**领跌板块**")
    lines.append("")
    lines.append("| 板块 | 涨跌幅 |")
    lines.append("| --- | --- |")
    for s in bottom:
        lines.append(f"| {s.get('name', '-')} | {s.get('change_pct', 0):+.2f}% |")
    lines.append("")
    return "\n".join(lines)


def build_highlights_section(pool: Optional[List[Dict[str, Any]]]) -> str:
    """今日之最。"""
    lines = ["## 五、今日之最", ""]
    if not pool:
        lines.append("> 涨停池为空。")
        lines.append("")
        return "\n".join(lines)

    def _safe(items, key, reverse=True):
        vals = [x for x in items if x.get(key) is not None]
        if not vals:
            return None
        return sorted(vals, key=lambda x: x.get(key) or 0, reverse=reverse)[0]

    highest_board = _safe(pool, "consecutive_boards")
    biggest_seal = _safe(pool, "seal_amount")
    highest_turnover = _safe(pool, "turnover_rate")

    if highest_board:
        lines.append(
            f"- **最高连板**：{highest_board.get('name')} "
            f"（{highest_board.get('consecutive_boards')} 连板，{highest_board.get('industry') or '未分类'}）"
        )
    if biggest_seal:
        lines.append(
            f"- **最硬封板（封单最大）**：{biggest_seal.get('name')} "
            f"（封板资金 {_fmt_seal(biggest_seal.get('seal_amount'))}）"
        )
    if highest_turnover:
        turnover = highest_turnover.get("turnover_rate")
        turnover_str = f"{turnover:.1f}%" if turnover is not None else "-"
        lines.append(
            f"- **最高换手（资金博弈最激烈）**：{highest_turnover.get('name')} "
            f"（换手率 {turnover_str}）"
        )
    lines.append("")
    return "\n".join(lines)


def generate_review(date: str) -> str:
    """生成完整复盘底稿 Markdown。"""
    manager = DataFetcherManager()

    print("[1/5] 获取市场涨跌统计...", file=sys.stderr)
    stats = manager.get_market_stats(purpose="limit_up_review") or {}

    print("[2/5] 获取跌停池与炸板池统计...", file=sys.stderr)
    negative_counts = fetch_negative_board_counts(date=date)
    break_count = apply_negative_board_counts(stats, negative_counts)

    print("[3/5] 获取涨停池明细（全量）...", file=sys.stderr)
    pool = fetch_limit_up_pool(manager, date=date)
    if pool:
        stats["limit_up_count"] = len(pool)
        print(f"     涨停池共 {len(pool)} 只", file=sys.stderr)
    else:
        print("[警告] 涨停池为空，涨停梯队和题材分布将跳过", file=sys.stderr)

    print("[4/5] 获取板块涨跌榜...", file=sys.stderr)
    rankings = manager.get_sector_rankings(n=5)

    if break_count is None:
        print("[5/5] 炸板池未取到，炸板率将显示 N/A", file=sys.stderr)
    else:
        print(f"[5/5] 炸板池统计完成: {break_count} 只", file=sys.stderr)

    pretty_date = datetime.strptime(date, "%Y%m%d").strftime("%Y-%m-%d")

    parts = [
        f"# {pretty_date} 涨停复盘底稿",
        "",
        "> 本文由 daily_stock_analysis 数据脚本自动生成，仅为复盘原始素材，请自行核对与加工。",
        "",
        build_sentiment_section(stats, break_count),
        build_ladder_section(pool),
        build_theme_section(pool),
        build_sector_section(rankings),
        build_highlights_section(pool),
        "---",
        "",
        "**以上为个人复盘思考，不构成任何投资建议，股市有风险，投资需谨慎。**",
        "",
    ]
    return "\n".join(parts)


def send_review(markdown: str, date: str) -> bool:
    """复用 DSA 通知服务推送复盘底稿。"""
    try:
        from src.notification import NotificationService

        notifier = NotificationService()
        result = notifier.send_with_results(
            markdown,
            email_send_to_all=True,
            route_type="report",
            severity="info",
            dedup_key=f"limit_up_review:{date}",
            cooldown_key="limit_up_review",
        )
        channels = ", ".join(
            f"{item.channel}:{'ok' if item.success else 'fail'}"
            for item in result.channel_results
        )
        print(
            f"[推送] status={result.status}, success={result.success}, channels={channels}",
            file=sys.stderr,
        )
        return bool(result.success)
    except Exception as exc:
        print(f"[错误] 复盘推送失败: {exc}", file=sys.stderr)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="生成涨停复盘 Markdown 底稿")
    parser.add_argument(
        "--date",
        default=datetime.now().strftime("%Y%m%d"),
        help="复盘日期，格式 YYYYMMDD，默认今天",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="输出文件路径，不指定则只打印到标准输出",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="生成后通过 DSA 已配置的报告通知渠道推送",
    )
    args = parser.parse_args()

    try:
        datetime.strptime(args.date, "%Y%m%d")
    except ValueError:
        print(f"[错误] 日期格式应为 YYYYMMDD，收到：{args.date}", file=sys.stderr)
        return 1

    markdown = generate_review(args.date)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(markdown, encoding="utf-8")
        print(f"[完成] 已写入 {out_path.resolve()}", file=sys.stderr)

    if args.send:
        if not send_review(markdown, args.date):
            return 2

    if not args.out and not args.send:
        print(markdown)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
