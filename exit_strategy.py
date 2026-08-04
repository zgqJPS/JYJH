"""
exit_strategy.py - 出场策略系统
================================
基于历史数据和市场状态，提供明确的出场信号。

出场信号分两类：
1. 个股层面：针对已持有的个股，判断是否应该卖出
2. 市场层面：整体市场环境恶化，建议减仓或清仓

核心原则：
- 止盈优先：达到目标收益或出现见顶信号时及时止盈
- 止损坚决：跌破止损线立即出场，不抱幻想
- 信号驱动：基于客观数据信号，不依赖主观判断

用法:
  python exit_strategy.py                    # 检测当前出场信号
  python exit_strategy.py --date 2026-07-30  # 检测指定日期
  python exit_strategy.py --holding 003032   # 检测指定持仓股票
"""

import sqlite3
import json
import os
import sys
import logging
import argparse
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple

from config import DB_PATH

# ─────────────────────────── 日志 ───────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('exit_strategy')

# ─────────────────────────── 数据库工具 ───────────────────────────

def get_conn(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def get_latest_date(db_path: str = DB_PATH, table: str = 'xgt_limit_up_detail') -> Optional[str]:
    try:
        conn = get_conn(db_path)
        cursor = conn.execute(f"SELECT MAX(date) as max_date FROM {table}")
        result = cursor.fetchone()
        conn.close()
        return result['max_date'] if result and result['max_date'] else None
    except Exception as e:
        logger.error(f"获取最新日期失败: {e}")
        return None

# ─────────────────────────── 数据查询 ───────────────────────────

def get_stock_history(stock_code: str, days: int = 10, db_path: str = DB_PATH) -> List[Dict]:
    try:
        conn = get_conn(db_path)
        cursor = conn.execute("""
            SELECT * FROM xgt_limit_up_detail
            WHERE code = ?
            ORDER BY date DESC
            LIMIT ?
        """, (stock_code, days))
        results = [dict(row) for row in cursor.fetchall()]
        conn.close()
        results.reverse()
        return results
    except Exception as e:
        logger.error(f"获取股票历史失败 {stock_code}: {e}")
        return []

def get_daily_summary(date: str, db_path: str = DB_PATH) -> Optional[Dict]:
    try:
        conn = get_conn(db_path)
        cursor = conn.execute("""
            SELECT * FROM xgt_daily_summary
            WHERE date = ?
        """, (date,))
        result = cursor.fetchone()
        conn.close()
        return dict(result) if result else None
    except Exception as e:
        logger.error(f"获取每日汇总失败: {e}")
        return None

def get_smash_coefficient(date: str, db_path: str = DB_PATH) -> Optional[float]:
    try:
        conn = get_conn(db_path)
        cursor = conn.execute("""
            SELECT smash_coefficient FROM smash_coefficients
            WHERE trade_date = ?
        """, (date,))
        result = cursor.fetchone()
        conn.close()
        return result['smash_coefficient'] if result else None
    except Exception as e:
        logger.error(f"获取砸盘系数失败: {e}")
        return None

def get_limit_up_stocks(date: str, db_path: str = DB_PATH) -> List[Dict]:
    try:
        conn = get_conn(db_path)
        cursor = conn.execute("""
            SELECT * FROM xgt_limit_up_detail
            WHERE date = ?
            ORDER BY limit_up_days DESC, first_limit_up_time ASC
        """, (date,))
        results = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return results
    except Exception as e:
        logger.error(f"获取涨停股票失败: {e}")
        return []

def get_break_limit_up_stocks(date: str, db_path: str = DB_PATH) -> List[Dict]:
    try:
        conn = get_conn(db_path)
        cursor = conn.execute("""
            SELECT * FROM xgt_break_limit_up
            WHERE date = ?
            ORDER BY first_limit_up_time ASC
        """, (date,))
        results = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return results
    except Exception as e:
        logger.error(f"获取炸板股票失败: {e}")
        return []

# ─────────────────────────── 出场信号检测（个股层面） ───────────────────────────

def check_stock_exit_signals(stock_code: str, stock_name: str = None, 
                             holding_days: int = 0, buy_price: float = None,
                             db_path: str = DB_PATH) -> Dict[str, Any]:
    logger.info(f"检测个股出场信号: {stock_code} {stock_name or ''}")
    history = get_stock_history(stock_code, days=10, db_path=db_path)
    if not history:
        return {
            'stock_code': stock_code,
            'stock_name': stock_name or '',
            'exit_signals': [],
            'exit_recommended': False,
            'exit_urgency': 'NONE',
            'suggestion': '未找到该股票的涨停记录'
        }
    latest = history[-1]
    latest_date = latest['date']
    current_boards = latest.get('limit_up_days', 1)
    if not stock_name:
        stock_name = latest.get('name', '')
    exit_signals = []
    if len(history) >= 2:
        prev = history[-2]
        prev_boards = prev.get('limit_up_days', 1)
        curr_boards_for_check = latest.get('limit_up_days', 1)
        if curr_boards_for_check < prev_boards:
            exit_signals.append({
                'signal_type': 'BOARD_BREAK',
                'signal_name': '断板出场',
                'severity': 'CRITICAL',
                'description': f"前一日{prev_boards}板，今日仅{curr_boards_for_check}板，连板中断",
                'action': '立即出场，连板中断意味着龙头地位丧失'
            })
    if latest.get('break_times', 0) >= 5:
        exit_signals.append({
            'signal_type': 'HEAVY_BREAKS',
            'signal_name': '严重开板',
            'severity': 'CRITICAL',
            'description': f"今日开板{latest['break_times']}次，封板极不稳定",
            'action': '立即出场，封板力度严重不足'
        })
    if len(history) >= 2:
        prev = history[-2]
        prev_seal = prev.get('seal_ratio', 0) or 0
        curr_seal = latest.get('seal_ratio', 0) or 0
        if prev_seal >= 0.05 and curr_seal < 0.02:
            decline_pct = (prev_seal - curr_seal) / prev_seal * 100 if prev_seal > 0 else 0
            exit_signals.append({
                'signal_type': 'SEAL_DECLINE',
                'signal_name': '封单比骤降',
                'severity': 'HIGH',
                'description': f"封单比从{prev_seal:.2%}降至{curr_seal:.2%}（降幅{decline_pct:.0f}%）",
                'action': '警惕封板力度减弱，考虑减仓或出场'
            })
    break_times = latest.get('break_times', 0) or 0
    if break_times >= 3:
        exit_signals.append({
            'signal_type': 'MULTIPLE_BREAKS',
            'signal_name': '多次开板',
            'severity': 'HIGH',
            'description': f"今日开板{break_times}次，封板不稳定",
            'action': '封板质量差，建议减仓或出场'
        })
    elif break_times >= 2:
        exit_signals.append({
            'signal_type': 'MULTIPLE_BREAKS',
            'signal_name': '多次开板',
            'severity': 'MEDIUM',
            'description': f"今日开板{break_times}次，需警惕",
            'action': '关注后续封板情况，如继续开板则出场'
        })
    turnover = latest.get('turnover_rate', 0) or 0
    if turnover > 30:
        exit_signals.append({
            'signal_type': 'HIGH_TURNOVER',
            'signal_name': '换手率过高',
            'severity': 'HIGH',
            'description': f"换手率{turnover:.1f}%，分歧极大",
            'action': '筹码松动，建议出场'
        })
    elif turnover > 20:
        exit_signals.append({
            'signal_type': 'HIGH_TURNOVER',
            'signal_name': '换手率偏高',
            'severity': 'MEDIUM',
            'description': f"换手率{turnover:.1f}%，分歧较大",
            'action': '关注明日走势，如继续高换手则出场'
        })
    if holding_days >= 3:
        recent_ok = 0
        for h in history[-min(3, len(history)):]:
            if (h.get('break_times', 0) or 0) <= 1:
                recent_ok += 1
        if recent_ok == 0:
            exit_signals.append({
                'signal_type': 'TIME_EXIT',
                'signal_name': '时间出场',
                'severity': 'MEDIUM',
                'description': f"持有{holding_days}天，近3天均有严重开板",
                'action': '短线不适合长期持有，建议出场'
            })
    if current_boards >= 5:
        if break_times > 0 or (latest.get('seal_ratio', 0) or 0) < 0.03:
            exit_signals.append({
                'signal_type': 'TARGET_REACHED',
                'signal_name': '目标板级见顶',
                'severity': 'MEDIUM',
                'description': f"已达{current_boards}板高位，出现见顶信号",
                'action': '高位风险加大，建议分批止盈'
            })
    current_profit = None
    if buy_price and buy_price > 0:
        estimated_price = buy_price * (1.1 ** (current_boards - 1))
        current_profit = (estimated_price - buy_price) / buy_price * 100
    exit_recommended = len(exit_signals) > 0
    severity_order = {'CRITICAL': 0, 'HIGH': 1, 'MEDIUM': 2, 'LOW': 3}
    if exit_signals:
        most_severe = min(exit_signals, key=lambda x: severity_order.get(x['severity'], 99))
        exit_urgency = most_severe['severity']
    else:
        exit_urgency = 'NONE'
    if not exit_signals:
        suggestion = f"当前无出场信号，可继续持有（{current_boards}板）"
    elif exit_urgency == 'CRITICAL':
        suggestion = f"⚠️ 出现紧急出场信号，建议立即出场"
    elif exit_urgency == 'HIGH':
        suggestion = f"⚠️ 出现高风险信号，建议尽快出场或减仓"
    elif exit_urgency == 'MEDIUM':
        suggestion = f"⚠️ 出现中等风险信号，建议关注明日走势，必要时出场"
    else:
        suggestion = f"出现出场信号，建议谨慎对待"
    if current_profit is not None:
        suggestion += f"\n当前浮盈约{current_profit:.1f}%"
    return {
        'stock_code': stock_code,
        'stock_name': stock_name,
        'latest_date': latest_date,
        'current_boards': current_boards,
        'exit_signals': exit_signals,
        'exit_recommended': exit_recommended,
        'exit_urgency': exit_urgency,
        'current_profit': current_profit,
        'suggestion': suggestion
    }

# ─────────────────────────── 出场信号检测（市场层面） ───────────────────────────

def check_market_exit_signals(date: str = None, db_path: str = DB_PATH) -> Dict[str, Any]:
    if date is None:
        date = get_latest_date(db_path=db_path)
    if not date:
        return {
            'date': None,
            'market_signals': [],
            'market_exit_recommended': False,
            'market_exit_urgency': 'NONE',
            'position_suggestion': '无法判断',
            'overall_suggestion': '未找到市场数据'
        }
    logger.info(f"检测市场出场信号: {date}")
    market_signals = []
    summary = get_daily_summary(date, db_path=db_path)
    if summary:
        explosion_rate = summary.get('explosion_rate', 0) or 0
        if explosion_rate > 0.45:
            market_signals.append({
                'signal_type': 'EXPLOSION_RATE_SURGE',
                'signal_name': '炸板率飙升',
                'severity': 'CRITICAL',
                'description': f"炸板率{explosion_rate:.1%}，极度危险",
                'action': '市场极度危险，建议清仓观望'
            })
        elif explosion_rate > 0.35:
            market_signals.append({
                'signal_type': 'EXPLOSION_RATE_SURGE',
                'signal_name': '炸板率飙升',
                'severity': 'HIGH',
                'description': f"炸板率{explosion_rate:.1%}，超过35%警戒线",
                'action': '市场风险加大，建议减仓至3成以内'
            })
        elif explosion_rate > 0.25:
            market_signals.append({
                'signal_type': 'EXPLOSION_RATE_SURGE',
                'signal_name': '炸板率偏高',
                'severity': 'MEDIUM',
                'description': f"炸板率{explosion_rate:.1%}，偏高",
                'action': '谨慎参与，仓位控制在5成以内'
            })
    smash = get_smash_coefficient(date, db_path=db_path)
    if smash is not None:
        if smash > 7.0:
            market_signals.append({
                'signal_type': 'SMASH_SURGE',
                'signal_name': '砸盘系数极高',
                'severity': 'CRITICAL',
                'description': f"砸盘系数{smash:.2f}，抛压极大",
                'action': '市场抛压极大，建议清仓观望'
            })
        elif smash > 6.0:
            market_signals.append({
                'signal_type': 'SMASH_SURGE',
                'signal_name': '砸盘系数骤升',
                'severity': 'HIGH',
                'description': f"砸盘系数{smash:.2f}，抛压沉重",
                'action': '高位股抛压大，建议减仓防守'
            })
        elif smash > 5.0:
            market_signals.append({
                'signal_type': 'SMASH_SURGE',
                'signal_name': '砸盘系数偏高',
                'severity': 'MEDIUM',
                'description': f"砸盘系数{smash:.2f}，注意抛压",
                'action': '谨慎追高，控制仓位'
            })
    limit_up_stocks = get_limit_up_stocks(date, db_path=db_path)
    if limit_up_stocks:
        max_boards = max(s.get('limit_up_days', 1) for s in limit_up_stocks)
        yesterday = (datetime.strptime(date, '%Y-%m-%d') - timedelta(days=1)).strftime('%Y-%m-%d')
        yesterday_stocks = get_limit_up_stocks(yesterday, db_path=db_path)
        if yesterday_stocks:
            yesterday_max = max(s.get('limit_up_days', 1) for s in yesterday_stocks)
            yesterday_dragon = [s for s in yesterday_stocks if s.get('limit_up_days', 1) == yesterday_max]
            if yesterday_dragon:
                dragon_code = yesterday_dragon[0]['code']
                today_still_limit = any(s['code'] == dragon_code for s in limit_up_stocks)
                if not today_still_limit:
                    market_signals.append({
                        'signal_type': 'DRAGON_BREAK',
                        'signal_name': '龙头断板',
                        'severity': 'HIGH',
                        'description': f"昨日龙头（{yesterday_max}板）今日断板",
                        'action': '市场可能进入冰点，建议减仓或清仓'
                    })
    if limit_up_stocks:
        boards_2_plus = [s for s in limit_up_stocks if s.get('limit_up_days', 1) >= 2]
        if len(boards_2_plus) == 0 and len(limit_up_stocks) > 0:
            market_signals.append({
                'signal_type': 'TIER_GAP',
                'signal_name': '连板梯队断层',
                'severity': 'MEDIUM',
                'description': f"2板以上无股，仅首板活跃",
                'action': '梯队断档，市场高度受限，谨慎参与'
            })
    if summary:
        limit_up_count = summary.get('limit_up_count', 0) or 0
        if limit_up_count < 30:
            market_signals.append({
                'signal_type': 'SENTIMENT_LOW',
                'signal_name': '情绪冰点',
                'severity': 'MEDIUM',
                'description': f"涨停数{limit_up_count}只，低于30只警戒线",
                'action': '市场情绪低迷，轻仓试错或观望'
            })
    market_exit_recommended = len(market_signals) > 0
    severity_order = {'CRITICAL': 0, 'HIGH': 1, 'MEDIUM': 2, 'LOW': 3}
    if market_signals:
        most_severe = min(market_signals, key=lambda x: severity_order.get(x['severity'], 99))
        market_exit_urgency = most_severe['severity']
    else:
        market_exit_urgency = 'NONE'
    if market_exit_urgency == 'CRITICAL':
        position_suggestion = '建议仓位：空仓或极轻仓（1成以内）'
    elif market_exit_urgency == 'HIGH':
        position_suggestion = '建议仓位：3成以内'
    elif market_exit_urgency == 'MEDIUM':
        position_suggestion = '建议仓位：5成以内'
    else:
        position_suggestion = '建议仓位：可正常参与（5-8成）'
    if not market_signals:
        overall_suggestion = "市场状态正常，可正常参与"
    elif market_exit_urgency == 'CRITICAL':
        overall_suggestion = "⚠️ 市场极度危险，建议清仓观望，等待新的机会"
    elif market_exit_urgency == 'HIGH':
        overall_suggestion = "⚠️ 市场风险加大，建议减仓防守，等待风险释放"
    elif market_exit_urgency == 'MEDIUM':
        overall_suggestion = "⚠️ 市场出现风险信号，建议谨慎参与，控制仓位"
    else:
        overall_suggestion = "市场出现一些风险信号，建议谨慎对待"
    return {
        'date': date,
        'market_signals': market_signals,
        'market_exit_recommended': market_exit_recommended,
        'market_exit_urgency': market_exit_urgency,
        'position_suggestion': position_suggestion,
        'overall_suggestion': overall_suggestion
    }

# ─────────────────────────── 综合出场建议 ───────────────────────────

def generate_exit_advice(holdings: List[Dict] = None, date: str = None, 
                         db_path: str = DB_PATH) -> Dict[str, Any]:
    if date is None:
        date = get_latest_date(db_path=db_path)
    market_advice = check_market_exit_signals(date, db_path=db_path)
    stock_advices = []
    if holdings:
        for h in holdings:
            stock_advice = check_stock_exit_signals(
                stock_code=h['stock_code'],
                stock_name=h.get('stock_name'),
                holding_days=h.get('holding_days', 0),
                buy_price=h.get('buy_price'),
                db_path=db_path
            )
            stock_advices.append(stock_advice)
    overall_action = 'NORMAL'
    if market_advice['market_exit_urgency'] == 'CRITICAL':
        overall_action = 'CLEAR_ALL'
    elif market_advice['market_exit_urgency'] == 'HIGH':
        overall_action = 'REDUCE'
    elif any(s['exit_urgency'] in ['CRITICAL', 'HIGH'] for s in stock_advices):
        overall_action = 'REDUCE'
    elif any(s['exit_recommended'] for s in stock_advices):
        overall_action = 'HOLD'
    summary_parts = []
    if market_advice['market_exit_urgency'] in ['CRITICAL', 'HIGH']:
        summary_parts.append(f"市场风险加大（{market_advice['market_exit_urgency']}），{market_advice['position_suggestion']}")
    critical_stocks = [s for s in stock_advices if s['exit_urgency'] == 'CRITICAL']
    if critical_stocks:
        stock_names = '、'.join([f"{s['stock_name']}({s['stock_code']})" for s in critical_stocks])
        summary_parts.append(f"{stock_names}出现紧急出场信号，建议立即出场")
    high_risk_stocks = [s for s in stock_advices if s['exit_urgency'] == 'HIGH']
    if high_risk_stocks:
        stock_names = '、'.join([f"{s['stock_name']}({s['stock_code']})" for s in high_risk_stocks])
        summary_parts.append(f"{stock_names}出现高风险信号，建议尽快出场")
    if not summary_parts:
        if market_advice['market_exit_recommended']:
            summary_parts.append("市场出现风险信号，建议谨慎参与")
        else:
            summary_parts.append("市场状态正常，可正常参与")
    summary = '；'.join(summary_parts)
    return {
        'date': date,
        'market_advice': market_advice,
        'stock_advices': stock_advices,
        'overall_action': overall_action,
        'summary': summary
    }

# ─────────────────────────── 格式化输出 ───────────────────────────

def format_exit_report(advice: Dict) -> str:
    lines = []
    lines.append("=" * 60)
    lines.append(f"出场策略报告 - {advice['date']}")
    lines.append("=" * 60)
    market = advice['market_advice']
    lines.append("\n【市场环境】")
    lines.append(f"整体判断: {market['overall_suggestion']}")
    lines.append(f"仓位建议: {market['position_suggestion']}")
    if market['market_signals']:
        lines.append("\n市场信号:")
        for sig in market['market_signals']:
            severity_icon = {'CRITICAL': '🔴', 'HIGH': '🟠', 'MEDIUM': '🟡'}.get(sig['severity'], '⚪')
            lines.append(f"  {severity_icon} {sig['signal_name']}: {sig['description']}")
            lines.append(f"     → {sig['action']}")
    else:
        lines.append("\n市场信号: 无明显风险信号")
    if advice['stock_advices']:
        lines.append("\n" + "=" * 60)
        lines.append("【持仓个股】")
        for stock_advice in advice['stock_advices']:
            lines.append(f"\n{stock_advice['stock_name']}({stock_advice['stock_code']}) - {stock_advice['current_boards']}板")
            if stock_advice['current_profit'] is not None:
                lines.append(f"当前浮盈: {stock_advice['current_profit']:.1f}%")
            if stock_advice['exit_signals']:
                lines.append(f"出场信号 ({stock_advice['exit_urgency']}):")
                for sig in stock_advice['exit_signals']:
                    severity_icon = {'CRITICAL': '🔴', 'HIGH': '🟠', 'MEDIUM': '🟡'}.get(sig['severity'], '⚪')
                    lines.append(f"  {severity_icon} {sig['signal_name']}: {sig['description']}")
                    lines.append(f"     → {sig['action']}")
            else:
                lines.append("出场信号: 无")
            lines.append(f"建议: {stock_advice['suggestion']}")
    lines.append("\n" + "=" * 60)
    lines.append("【综合行动建议】")
    action_map = {
        'CLEAR_ALL': '🔴 清仓观望',
        'REDUCE': '🟠 减仓防守',
        'HOLD': '🟡 保持仓位，不出新仓',
        'NORMAL': '🟢 正常参与'
    }
    lines.append(f"行动: {action_map.get(advice['overall_action'], '未知')}")
    lines.append(f"\n总结: {advice['summary']}")
    lines.append("=" * 60)
    return '\n'.join(lines)

# ─────────────────────────── 主函数 ───────────────────────────

def main():
    parser = argparse.ArgumentParser(description='出场策略检测系统')
    parser.add_argument('--date', type=str, help='检测日期（默认最新日期）')
    parser.add_argument('--holding', type=str, help='检测指定持仓股票代码')
    parser.add_argument('--holding-name', type=str, help='持仓股票名称（可选）')
    parser.add_argument('--holding-days', type=int, default=0, help='持有天数')
    parser.add_argument('--buy-price', type=float, help='买入价格')
    args = parser.parse_args()
    if args.holding:
        result = check_stock_exit_signals(
            stock_code=args.holding,
            stock_name=args.holding_name,
            holding_days=args.holding_days,
            buy_price=args.buy_price
        )
        print("\n" + "=" * 60)
        print(f"个股出场信号检测: {result['stock_name']}({result['stock_code']})")
        print("=" * 60)
        if result['exit_signals']:
            print(f"\n出场信号 ({result['exit_urgency']}):")
            for sig in result['exit_signals']:
                severity_icon = {'CRITICAL': '🔴', 'HIGH': '🟠', 'MEDIUM': '🟡'}.get(sig['severity'], '⚪')
                print(f"  {severity_icon} {sig['signal_name']}: {sig['description']}")
                print(f"     → {sig['action']}")
        else:
            print("\n无出场信号")
        print(f"\n建议: {result['suggestion']}")
    else:
        holdings = []
        advice = generate_exit_advice(holdings=holdings, date=args.date)
        print(format_exit_report(advice))

if __name__ == '__main__':
    main()