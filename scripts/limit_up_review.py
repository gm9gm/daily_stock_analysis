#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
涨停复盘底稿生成脚本

收盘后拉取涨停池、市场情绪、板块榜数据，整理成一份 Markdown 复盘底稿，
方便直接复制到博客里再加工。数据全部复用项目内的 AkshareFetcher，
不依赖付费数据源。

输出内容：
    1. 市场情绪仪表盘（涨停/跌停/炸板率/成交额 等）
    2. 涨停梯队（按连板数从高到低分组）
    3. 题材分布（按所属行业聚合涨停家数）
    4. 主线板块榜（行业涨跌幅 Top/Bottom）
    5. 今日之最（最高封板资金 / 最高连板 / 最高换手）

使用方法：
    python scripts/limit_up_review.py                 # 复盘当天（默认全量涨停池）
    python scripts/limit_up_review.py --date 20260609  # 复盘指定日期
    python scripts/limit_up_review.py --out review.md   # 同时写入文件
    python scripts/limit_up_review.py --send            # 生成后推送到报告通知渠道
    python scripts/limit_up_review.py --top 30          # 仅取涨停池前 30 只（博客精简版）

说明：
    - 涨停池、炸板池数据为收盘后口径，盘中运行结果会不准。
    - 默认 --top 0 表示拉取当日涨停池全量；正整数则只保留前 N 只。
    - 涨跌停家数优先取自东财涨跌停池（与梯队明细同源），不再用全市场行情推算。
    - 炸板率 = 炸板数 / (涨停家数 + 炸板数)，分母与涨停池口径一致。
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

from data_provider.akshare_fetcher import AkshareFetcher


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


def apply_limit_pool_counts(
    stats: Dict[str, Any],
    pool_counts: Optional[Dict[str, int]],
) -> Optional[int]:
    """用东财涨跌停池覆盖推算值，并返回炸板数。"""
    if not pool_counts:
        return None

    if pool_counts.get("limit_up_count") is not None:
        stats["limit_up_count"] = pool_counts["limit_up_count"]
    if pool_counts.get("limit_down_count") is not None:
        stats["limit_down_count"] = pool_counts["limit_down_count"]
    return pool_counts.get("break_count")


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


def fetch_limit_pool_counts(date: str) -> Optional[Dict[str, int]]:
    """自包含获取东财涨停/跌停/炸板池统计，不依赖项目源码改动。"""
    try:
        import akshare as ak
    except Exception as exc:
        print(f"[警告] akshare 导入失败，无法获取涨跌停池统计: {exc}", file=sys.stderr)
        return None

    api_groups = {
        "limit_up_count": ("涨停池", ["stock_zt_pool_em"]),
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


# 全量拉取时传给 get_limit_up_pool 的上限（正常交易日远低于此值）
_FULL_POOL_FETCH_LIMIT = 10000


def fetch_limit_up_pool(
    fetcher: AkshareFetcher,
    date: str,
    top: int,
) -> Optional[List[Dict[str, Any]]]:
    """拉取涨停池；top<=0 为全量，正整数为前 N 只。"""
    pool_n = _FULL_POOL_FETCH_LIMIT if top <= 0 else top
    for attempt in range(1, 4):
        pool = fetcher.get_limit_up_pool(date=date, n=pool_n)
        if pool is not None:
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
    top: int,
) -> str:
    """涨停梯队，按连板数从高到低分组。"""
    lines = ["## 二、涨停梯队（按连板数）", ""]
    if not pool:
        lines.append("> 涨停池为空或获取失败。")
        lines.append("")
        return "\n".join(lines)

    scope = "全量涨停池" if top <= 0 else f"前 {top} 只"
    lines.append(f"> 以下共 **{len(pool)}** 只（{scope}）。")
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


def generate_review(date: str, top: int) -> str:
    """生成完整复盘底稿 Markdown。"""
    fetcher = AkshareFetcher()

    print("[1/5] 获取市场涨跌统计...", file=sys.stderr)
    stats = fetcher.get_market_stats() or {}

    print("[2/5] 获取东财涨跌停池统计...", file=sys.stderr)
    pool_counts = fetch_limit_pool_counts(date=date)
    break_count = apply_limit_pool_counts(stats, pool_counts)

    pool_label = "全量" if top <= 0 else f"前 {top} 只"
    print(f"[3/5] 获取涨停池明细（{pool_label}）...", file=sys.stderr)
    pool = fetch_limit_up_pool(fetcher, date=date, top=top)
    if pool and top <= 0 and stats.get("limit_up_count") != len(pool):
        print(
            f"[提示] 涨停家数改用涨停池明细数量: {stats.get('limit_up_count')} -> {len(pool)}",
            file=sys.stderr,
        )
        stats["limit_up_count"] = len(pool)

    print("[4/5] 获取板块涨跌榜...", file=sys.stderr)
    rankings = fetcher.get_sector_rankings(n=5)

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
        build_ladder_section(pool, top),
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
        "--top",
        type=int,
        default=0,
        help="涨停池条数：0=全量（默认），正整数=仅取前 N 只",
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

    markdown = generate_review(args.date, args.top)

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
