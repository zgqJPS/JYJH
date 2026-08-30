#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
volume_price_analyzer.py — 整体量价走势分析引擎（筛选与进场决策的首要依据）

设计背景（实盘教训）：
    打板/接力的核心亏损形态往往不是"题材不好"或"封单不大"，而是量价结构恶化：
    高位缩量一字（筹码未交换，排到即接盘）、连板途中量能阶梯断裂、
    放量滞涨/尾盘偷袭、炸板放量不回封（一致转分歧）。
    因此将"整体量价走势"提升为筛选与进场的第一道闸门：
    量价结构不合格的标的，其他维度再强也不进入推荐/不给买点。

分析层级：
    1. 个股量价形态 analyze_stock_volume_price(stock, history, market_state)
       - 连板量能阶梯（量比/换手在连板途中的演化）
       - 封单-量能配合（封单强 vs 放量/缩量是否健康）
       - 换手轨迹（逐板换手是否充分交换、高位巨量出货嫌疑）
       - 分歧节奏（开板-回封的量价含义，与分歧/一致节奏联动）
       - 当日量价定性（缩量加速/温和放量/放量滞涨/巨量分歧/尾盘偷袭）
       输出：grade(pass/caution/fail)、score(0-100)、pattern、一票否决项
    2. 市场量价环境 analyze_market_volume_price(date, db_path)
       - 涨停家数趋势 + 平均量比 + 高位股存活率
       - 炸板率与封板率、跌停家数
       - 量价背离信号（指数/涨停数 vs 赚钱效应）
       输出：grade、score、状态标签、闸门提示

硬性规则（一票否决，fail）：
    - 高位(>=4板)一字缩量且非总龙头（沿用既定纪律）
    - 连板途中量能骤断（今日量比<0.6 且 板数>=2，资金断档）
    - 高位巨量换手（板数>=4 且 换手>35%，出货嫌疑）
    - 当日巨量不封/尾盘偷袭（量比>5 且 14:00后封板 或 炸板>=3）
    - 一致转分歧（开板>=1 且 封单比<1%，封不住）

注意：换手率字段数据源口径不一致（小数/百分数混用），统一清洗为小数。
"""

import sqlite3
import logging
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime

from config import DB_PATH

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════════

def _safe_float(val, default=None):
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _safe_turnover(tr) -> Optional[float]:
    """换手率清洗为小数（0.15 = 15%）；缺失/0/异常返回 None（0换手不可能，视为缺失）"""
    if tr is None:
        return None
    try:
        v = float(tr)
        if v > 1.0:          # 百分数形式（2.2 表示 2.2%）
            v = v / 100.0
        if v > 1.0 or v <= 0:
            return None
        return v
    except (TypeError, ValueError):
        return None


def _parse_time_minutes(t_str) -> Optional[int]:
    """封板时间 → 分钟数（9:30=570, 14:00=840, 15:00=900）"""
    if not t_str or str(t_str).strip() in ('', 'None'):
        return None
    s = str(t_str).strip()
    try:
        if ':' in s:
            parts = s.split(':')
            return int(parts[0]) * 60 + int(parts[1])
        if len(s) >= 4:
            s = s.zfill(6)
            return int(s[:2]) * 60 + int(s[2:4])
    except (ValueError, IndexError):
        return None
    return None


def _is_yizi(stock: Dict) -> bool:
    """一字缩量板：9:35前封死且全天0开板（换手率口径不作硬判据）"""
    if (stock.get('break_times') or 0) != 0:
        return False
    t = _parse_time_minutes(stock.get('first_limit_up_time'))
    return t is not None and t <= 575


# ══════════════════════════════════════════════════════════════
# 个股量价形态分析
# ══════════════════════════════════════════════════════════════

def analyze_stock_volume_price(stock: Dict,
                               history: Optional[List[Dict]] = None,
                               market_state: Optional[Dict] = None,
                               is_total_dragon: bool = False) -> Dict[str, Any]:
    """
    评估个股"整体量价走势"，作为筛选/进场的首要依据。

    参数:
        stock: 当日 xgt_limit_up_detail 记录（dict）
        history: 该个股近期涨停记录列表（按日期倒序，元素为当日同结构 dict）
        market_state: 市场状态（含 smash_coefficient / explosion_rate）
        is_total_dragon: 是否市场总龙头（总龙头可豁免部分高位一字限制）

    返回:
        {
          'grade': 'pass'|'caution'|'fail',
          'score': 0-100,
          'pattern': 形态标签,
          'signals': [正面量价信号],
          'risks': [风险量价信号],
          'veto_reasons': [一票否决原因],   # 非空即 fail
          'metrics': {关键量价指标},
          'action_gate': '可参与'|'仅观察'|'禁止参与',
        }
    """
    history = history or []
    market_state = market_state or {}

    boards = int(stock.get('limit_up_days', 1) or 1)
    volume_bias = _safe_float(stock.get('volume_bias'), 1.0)
    turnover = _safe_turnover(stock.get('turnover_rate'))
    seal_ratio = _safe_float(stock.get('seal_ratio'), 0.0) or 0.0
    break_times = int(stock.get('break_times') or 0)
    first_min = _parse_time_minutes(stock.get('first_limit_up_time'))
    yizi = _is_yizi(stock)

    signals: List[str] = []
    risks: List[str] = []
    veto: List[str] = []
    score = 50.0

    # ── 0. 炸板池股票（当日未封住涨停，一致转分歧）：直接否决 ──
    # 炸板池的 seal_ratio/volume_bias/turnover 字段为占位默认值，不具分析意义；
    # 且"封住涨停"是打板/接力体系的最低门槛，未封板=无买点=卖点。
    if stock.get('_from_break_pool'):
        return {
            'grade': 'fail', 'score': 10.0,
            'pattern': '炸板未回封（一致转分歧）',
            'signals': [],
            'risks': [f'全天炸板{break_times}次未封住，封单量价数据无效'],
            'veto_reasons': ['炸板未回封：未封住涨停=无买点，持仓者按离场纪律处理'],
            'action_gate': '禁止参与',
            'metrics': {
                'boards': boards, 'volume_bias': volume_bias,
                'turnover_rate': turnover, 'seal_ratio': seal_ratio,
                'break_times': break_times, 'first_seal_minutes': first_min,
                'is_yizi': False, 'volume_trend': 'flat', 'market_gate': False,
            },
        }

    # ── 1. 连板量能阶梯（今日 vs 连板途中量能）──
    # 健康连板：启动日放量换手 → 加速日温和缩量或平量；
    # 危险形态：高位突然巨量（出货）或量能骤断（资金断档）。
    hist = [h for h in history if _safe_float(h.get('volume_bias')) is not None]
    prev_vb = _safe_float(hist[0].get('volume_bias')) if hist else None
    vb_trend = 'flat'
    if prev_vb and volume_bias:
        ratio = volume_bias / prev_vb if prev_vb > 0 else 1.0
        if ratio >= 1.8:
            vb_trend = 'surge'    # 骤然放量
        elif ratio <= 0.6:
            vb_trend = 'collapse' # 量能骤断
        elif ratio >= 1.2:
            vb_trend = 'up'
        elif ratio <= 0.85:
            vb_trend = 'shrink'

    # 量比绝对水平
    if 1.3 <= volume_bias <= 3.0:
        score += 12
        signals.append(f"量比{volume_bias:.1f}（温和放量，量价配合健康）")
    elif 0.8 <= volume_bias < 1.3:
        score += 6
        signals.append(f"量比{volume_bias:.1f}（平量封板，筹码稳定）")
    elif volume_bias < 0.8:
        if yizi and boards <= 3:
            score += 8
            signals.append(f"量比{volume_bias:.1f}（低位缩量一字，惜售）")
        elif boards >= 2:
            score -= 8
            risks.append(f"量比{volume_bias:.1f}（量能骤缩，接力资金断档风险）")
        else:
            score += 2
    elif 3.0 < volume_bias <= 5.0:
        score += 0
        risks.append(f"量比{volume_bias:.1f}（明显放量，分歧加大）")
    else:  # >5
        score -= 12
        risks.append(f"量比{volume_bias:.1f}（巨量，多空激烈搏杀）")

    # 量能趋势
    if vb_trend == 'surge' and boards >= 3:
        score -= 10
        risks.append("连板高位量能骤然放大（对倒/出货嫌疑）")
    elif vb_trend == 'collapse' and boards >= 2:
        score -= 6
        risks.append("量能较前日骤断（封板无新增资金，持续性存疑）")
    elif vb_trend == 'shrink' and boards <= 4 and break_times == 0:
        score += 6
        signals.append("连板途中温和缩量（筹码锁定，加速健康）")

    # ── 2. 换手轨迹（逐板换手是否充分）──
    prev_turnover = _safe_turnover(hist[0].get('turnover_rate')) if hist else None
    if turnover is None:
        score -= 5
        risks.append("换手率数据异常/缺失")
    else:
        if boards <= 2:
            if 0.05 <= turnover <= 0.20:
                score += 12
                signals.append(f"换手{turnover:.1%}（启动期充分交换，健康）")
            elif turnover < 0.03:
                score += 4
                signals.append(f"换手{turnover:.1%}（缩量一致，低位可排队）")
            elif turnover > 0.30:
                score -= 10
                risks.append(f"换手{turnover:.1%}（首板即巨量，抛压过重）")
            else:
                score += 6
        else:  # 高位板
            if 0.10 <= turnover <= 0.30:
                score += 12
                signals.append(f"换手{turnover:.1%}（高位充分换手，承接有效）")
            elif turnover < 0.05:
                if yizi:
                    score -= 6
                    risks.append(f"换手{turnover:.1%}（高位缩量一字，筹码未交换）")
                else:
                    score += 2
            elif turnover > 0.35:
                score -= 15
                risks.append(f"换手{turnover:.1%}（高位巨量，出货特征）")
            else:
                score += 4
        # 换手骤增
        if prev_turnover and prev_turnover > 0 and boards >= 3:
            tr_ratio = turnover / prev_turnover
            if tr_ratio >= 2.5:
                score -= 10
                risks.append(f"换手较前日放大{tr_ratio:.1f}倍（高位放量危险）")

    # ── 3. 封单-量能配合 ──
    if seal_ratio >= 0.05 and volume_bias <= 3.0:
        score += 8
        signals.append(f"封单比{seal_ratio:.1%}+量能可控（封板坚决）")
    elif seal_ratio >= 0.05 and volume_bias > 5.0:
        score -= 6
        risks.append("大封单但巨量（封板中分歧激烈，封单易被砸穿）")
    elif seal_ratio < 0.01 and break_times >= 1:
        score -= 8
        risks.append(f"封单比仅{seal_ratio:.1%}且有开板（封单弱，回封无力）")

    # ── 4. 分歧节奏（开板-回封量价含义）──
    if break_times == 0:
        if yizi:
            if boards >= 4:
                if is_total_dragon:
                    score += 0
                    risks.append(f"{boards}板一字（总龙头：强势但防开板，开板放量即走）")
                else:
                    score -= 10
                    risks.append(f"{boards}板高位一字缩量（未经分歧，排到即接盘风险）")
            else:
                score += 8
                signals.append(f"{boards}板低位一字/秒板（资金抢筹）")
        else:
            score += 6
            signals.append("零开板封死（一致）")
    elif 1 <= break_times <= 2:
        if seal_ratio >= 0.02:
            score += 10
            signals.append(f"开板{break_times}次后放量回封（分歧转一致，经量价检验的买点）")
        else:
            score -= 4
            risks.append(f"开板{break_times}次但回封封单偏弱")
    else:
        score -= 15
        risks.append(f"全天炸板{break_times}次（高分歧，量价失控）")

    # ── 5. 当日量价定性 + 尾盘偷袭 ──
    pattern = _classify_pattern(volume_bias, turnover, seal_ratio,
                                break_times, first_min, boards, yizi)
    if first_min is not None and first_min >= 840 and volume_bias >= 3.0:
        score -= 12
        risks.append("尾盘放量封板（偷袭板，次日溢价低）")
    elif first_min is not None and first_min >= 840:
        score -= 5
        risks.append("尾盘封板（量价偏弱）")

    # ── 6. 一票否决（fail）──
    if boards >= 4 and yizi and not is_total_dragon:
        veto.append(f"{boards}板高位一字缩量（非总龙头）：能排到往往是开板接盘，等分歧回封")
    if boards >= 4 and turnover is not None and turnover > 0.35 and volume_bias > 4.0:
        veto.append(f"高位巨量换手{turnover:.0%}+量比{volume_bias:.1f}：出货特征，禁止追高")
    if break_times >= 3 and seal_ratio < 0.02:
        veto.append(f"炸板{break_times}次且封单弱（一致转分歧）：无买点，持仓应离场")
    if volume_bias > 5.0 and (first_min is None or first_min >= 840):
        veto.append("巨量+尾盘/未封板：放量滞涨，禁止参与")
    if boards >= 2 and volume_bias < 0.6 and not yizi:
        veto.append(f"量比{volume_bias:.1f}量能骤断：接力资金撤离，不参与")

    # 市场环境闸门（高砸盘/高炸板率下，除最强形态外一律收紧）
    smash = market_state.get('smash_coefficient')
    explosion = market_state.get('explosion_rate', 0) or 0
    market_gate = (smash is not None and smash >= 6.0) or explosion >= 0.35
    if market_gate:
        # 仅"分歧转一致回封"可保留，其余降级
        if not (1 <= break_times <= 2 and seal_ratio >= 0.02):
            veto.append(f"市场量价环境恶化（砸盘{smash if smash is not None else '?'}"
                        f"/炸板率{explosion:.0%}）：全场无买点，卖点纪律优先")

    score = max(0.0, min(100.0, score))

    if veto:
        grade = 'fail'
        gate = '禁止参与'
    elif score >= 62 and not risks:
        grade = 'pass'
        gate = '可参与'
    elif score >= 50:
        grade = 'caution'
        gate = '仅观察'
    elif score >= 40:
        grade = 'caution'
        gate = '仅观察'
    else:
        grade = 'fail'
        gate = '禁止参与'

    return {
        'grade': grade,
        'score': round(score, 1),
        'pattern': pattern,
        'signals': signals,
        'risks': risks,
        'veto_reasons': veto,
        'action_gate': gate,
        'metrics': {
            'boards': boards,
            'volume_bias': round(volume_bias, 2) if volume_bias else None,
            'turnover_rate': round(turnover, 4) if turnover is not None else None,
            'seal_ratio': round(seal_ratio, 4),
            'break_times': break_times,
            'first_seal_minutes': first_min,
            'is_yizi': yizi,
            'volume_trend': vb_trend,
            'market_gate': market_gate,
        },
    }


def _classify_pattern(volume_bias, turnover, seal_ratio, break_times,
                      first_min, boards, yizi) -> str:
    """当日量价形态定性标签"""
    if yizi and boards >= 4:
        return "高位缩量加速（一字）"
    if yizi:
        return "低位缩量一致（一字/秒板）"
    if break_times >= 3:
        return "巨量高分歧（炸板失控）"
    if 1 <= break_times <= 2 and seal_ratio >= 0.02:
        return "分歧放量回封（分歧转一致）"
    if volume_bias and volume_bias > 5.0:
        return "巨量搏杀（多空分歧）"
    if volume_bias and volume_bias < 0.8:
        return "缩量封板（惜售/断档待辨）"
    if first_min is not None and first_min >= 840:
        return "尾盘偷袭封板"
    if 1.3 <= (volume_bias or 0) <= 3.0:
        return "温和放量上攻（健康）"
    return "平量封板"


# ══════════════════════════════════════════════════════════════
# 市场量价环境分析
# ══════════════════════════════════════════════════════════════

def analyze_market_volume_price(date: str, db_path: str = DB_PATH,
                                lookback: int = 5) -> Dict[str, Any]:
    """
    市场整体量价环境（近 lookback 日趋势）。

    返回:
        {
          'grade': 'pass'|'caution'|'fail',
          'score': 0-100,
          'state_label': 状态标签,
          'signals': [], 'risks': [],
          'metrics': {涨停数趋势/平均量比/炸板率/高位存活/跌停数},
          'gate': '正常参与'|'收缩仓位'|'全场无买点',
        }
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # 近 lookback 个交易日
        date_rows = conn.execute("""
            SELECT DISTINCT date FROM xgt_limit_up_detail
            WHERE date <= ? ORDER BY date DESC LIMIT ?
        """, (date, lookback)).fetchall()
        dates = [r['date'] for r in date_rows]
        if not dates:
            return {'grade': 'caution', 'score': 50, 'state_label': '无数据',
                    'signals': [], 'risks': ['无涨停明细数据'],
                    'metrics': {}, 'gate': '收缩仓位'}

        daily_stats = []
        for d in dates:
            lu = conn.execute(
                "SELECT COUNT(*) c, AVG(volume_bias) avg_vb, "
                "AVG(turnover_rate) avg_tr FROM xgt_limit_up_detail WHERE date=?",
                (d,)).fetchone()
            summ = conn.execute(
                "SELECT limit_up_count, limit_down_count, break_limit_up_count, "
                "explosion_rate, max_continuous_boards FROM xgt_daily_summary WHERE date=?",
                (d,)).fetchone()
            sm = conn.execute(
                "SELECT smash_coefficient FROM smash_coefficients WHERE trade_date=?",
                (d,)).fetchone()
            # 跌停数：汇总表优先，缺失时从跌停池表计数兜底（避免缺失被误判为"无跌停恐慌"）
            ld_cnt = summ['limit_down_count'] if summ and summ['limit_down_count'] is not None else None
            if ld_cnt is None:
                try:
                    ld_row = conn.execute(
                        "SELECT COUNT(*) c FROM xgt_limit_down WHERE date=?", (d,)).fetchone()
                    ld_cnt = ld_row['c'] if ld_row else None
                except sqlite3.Error:
                    ld_cnt = None
            daily_stats.append({
                'date': d,
                'lu_count': (summ['limit_up_count'] if summ and summ['limit_up_count']
                             else (lu['c'] if lu else 0)),
                'limit_down': ld_cnt,
                'break_count': (summ['break_limit_up_count'] if summ else 0) or 0,
                'explosion_rate': (summ['explosion_rate'] if summ else None),
                'max_boards': (summ['max_continuous_boards'] if summ else 0) or 0,
                'avg_vb': lu['avg_vb'] if lu and lu['avg_vb'] else None,
                'smash': sm['smash_coefficient'] if sm else None,
            })

        today = daily_stats[0]
        prev = daily_stats[1] if len(daily_stats) > 1 else None

        signals: List[str] = []
        risks: List[str] = []
        score = 50.0

        # 涨停家数趋势
        lu_today = today['lu_count']
        lu_prev = prev['lu_count'] if prev else lu_today
        if lu_today >= 70:
            score += 12
            signals.append(f"涨停{lu_today}家（赚钱效应强）")
        elif lu_today >= 45:
            score += 6
            signals.append(f"涨停{lu_today}家（情绪尚可）")
        elif lu_today < 25:
            score -= 12
            risks.append(f"涨停仅{lu_today}家（赚钱效应冰点）")
        else:
            score += 0

        if lu_prev:
            chg = (lu_today - lu_prev) / max(lu_prev, 1)
            if chg <= -0.35 and lu_today < 50:
                score -= 10
                risks.append(f"涨停数较前日骤降{chg:.0%}（情绪退潮）")
            elif chg >= 0.35:
                score += 6
                signals.append(f"涨停数较前日+{chg:.0%}（情绪回暖）")

        # 炸板率
        er = today['explosion_rate']
        if er is not None:
            er = float(er)
            if er > 1.0:  # 百分数兼容
                er = er / 100.0
            if er >= 0.35:
                score -= 15
                risks.append(f"炸板率{er:.0%}（分歧极大，打板必谨慎）")
            elif er >= 0.25:
                score -= 6
                risks.append(f"炸板率{er:.0%}（偏高）")
            elif er <= 0.12:
                score += 8
                signals.append(f"炸板率{er:.0%}（封板稳定）")

        # 跌停家数（数据缺失不加分，避免把"无数据"误判为"无恐慌"）
        ld = today['limit_down']
        if ld is not None:
            if ld >= 50:
                score -= 15
                risks.append(f"跌停{ld}家（系统性风险）")
            elif ld >= 20:
                score -= 6
                risks.append(f"跌停{ld}家（亏钱效应扩散）")
            elif ld <= 5:
                score += 4
                signals.append(f"跌停仅{ld}家（无恐慌）")

        # 砸盘系数
        smash = today['smash']
        if smash is not None:
            if smash >= 6.5:
                score -= 15
                risks.append(f"砸盘系数{smash:.1f}（极高，全场无买点）")
            elif smash >= 5.5:
                score -= 8
                risks.append(f"砸盘系数{smash:.1f}（偏高，仅最强票可观察）")
            elif smash < 3.0:
                score += 6
                signals.append(f"砸盘系数{smash:.1f}（低位，分歧小）")

        # 高度梯队
        mb = today['max_boards']
        if mb >= 6:
            score += 4
            signals.append(f"市场高度{mb}板（打开空间）")
        elif mb <= 2 and lu_today < 50:
            score -= 6
            risks.append(f"最高板仅{mb}板（梯队断层/退潮）")

        # 平均量比（市场整体是否放量）
        if today['avg_vb']:
            avb = float(today['avg_vb'])
            if avb > 4.0:
                risks.append(f"涨停股平均量比{avb:.1f}（全场巨量分歧）")
                score -= 6
            elif 1.2 <= avb <= 2.5:
                signals.append(f"涨停股平均量比{avb:.1f}（放量有序）")
                score += 4

        score = max(0.0, min(100.0, score))

        # 闸门判定
        hard_gate = ((smash is not None and smash >= 6.5)
                     or (er is not None and er >= 0.40)
                     or (ld is not None and ld >= 80))
        if hard_gate:
            grade, gate, label = 'fail', '全场无买点', '量价恶化·风险释放'
        elif score >= 60:
            grade, gate, label = 'pass', '正常参与', '量价健康·可操作'
        elif score >= 42:
            grade, gate, label = 'caution', '收缩仓位', '量价分化·精选强票'
        else:
            grade, gate, label = 'fail', '全场无买点', '量价退潮·观望'

        return {
            'grade': grade,
            'score': round(score, 1),
            'state_label': label,
            'signals': signals,
            'risks': risks,
            'gate': gate,
            'metrics': {
                'date': dates[0],
                'limit_up_count': lu_today,
                'limit_up_prev': lu_prev,
                'limit_down_count': ld,
                'break_count': today['break_count'],
                'explosion_rate': round(er, 4) if er is not None else None,
                'max_boards': mb,
                'avg_volume_bias': round(float(today['avg_vb']), 2) if today['avg_vb'] else None,
                'smash_coefficient': smash,
            },
        }
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════
# 批量便捷接口
# ══════════════════════════════════════════════════════════════

def load_stock_history(conn, code: str, date: str, days: int = 10) -> List[Dict]:
    """加载个股近期涨停记录（倒序）"""
    rows = conn.execute("""
        SELECT date, limit_up_days, seal_ratio, turnover_rate,
               first_limit_up_time, break_times, volume_bias, concept
        FROM xgt_limit_up_detail
        WHERE code=? AND date < ? ORDER BY date DESC LIMIT ?
    """, (code, date, days)).fetchall()
    return [dict(r) for r in rows]


def analyze_date_volume_price(date: str, db_path: str = DB_PATH,
                              dragon_map: Optional[Dict] = None,
                              market_state: Optional[Dict] = None
                              ) -> Dict[str, Any]:
    """
    对某日全部涨停股做量价形态分析，返回 {market: 市场环境, stocks: {code: 个股结果}}
    dragon_map: {code: dragon_info}，用于总龙头豁免判断
    """
    dragon_map = dragon_map or {}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        market = analyze_market_volume_price(date, db_path)
        rows = conn.execute("""
            SELECT * FROM xgt_limit_up_detail WHERE date=?
        """, (date,)).fetchall()
        stocks: Dict[str, Any] = {}
        for r in rows:
            s = dict(r)
            code = s.get('code', '')
            hist = load_stock_history(conn, code, date)
            is_td = bool(dragon_map.get(code, {}).get('dragon_type') == 'total_dragon')
            stocks[code] = analyze_stock_volume_price(
                s, hist, market_state or {}, is_total_dragon=is_td)
        return {'date': date, 'market': market, 'stocks': stocks}
    finally:
        conn.close()


if __name__ == '__main__':
    import sys, json
    logging.basicConfig(level=logging.INFO)
    tgt = sys.argv[1] if len(sys.argv) > 1 else None
    if tgt:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT MAX(date) d FROM xgt_limit_up_detail").fetchone()
        d = tgt if tgt != 'latest' else row['d']
        result = analyze_date_volume_price(d)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        conn.close()
