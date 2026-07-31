"""
live_tracker.py - 实盘跟踪系统
================================
跟踪每日推荐的表现，验证信号准确性，生成每日跟踪报告。

主要功能:
  - track_daily(db)              每日跟踪：对比推荐与实际结果
  - evaluate_signals(db)         信号验证：检查信号触发及次日表现
  - generate_daily_report(db)    生成每日报告

用法:
  python live_tracker.py               # 运行今日跟踪
  python live_tracker.py --report      # 生成报告
  python live_tracker.py --date 2026-07-29  # 指定日期
"""

import sqlite3
import json
import os
import sys
import logging
import argparse
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, List, Any, Optional, Tuple

# ─────────────────────────── 日志 ───────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('live_tracker')

# ─────────────────────────── 常量 ───────────────────────────
def _auto_detect_db():
    """自动检测数据库路径"""
    import glob as _g
    _d = os.path.dirname(os.path.abspath(__file__))
    for f in _g.glob(os.path.join(_d, 'stock_data_*.db')):
        return f
    for f in _g.glob('stock_data_*.db'):
        return os.path.abspath(f)
    for f in _g.glob(os.path.join(_d, '..', 'stock_data_*.db')):
        return os.path.abspath(f)
    return 'stock_data.db'

DB_PATH = _auto_detect_db()
REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")

# 信号定义（1-8号信号）
SIGNAL_DEFINITIONS = {
    1: {
        'name': '龙头断板反转',
        'description': '最高连板股断板后次日，市场可能进入冰点→蓄力转换',
        'category': 'cycle',
    },
    2: {
        'name': '砸盘系数骤降',
        'description': '砸盘系数单日下降超过3.0，抛压骤减，可能迎来反弹',
        'category': 'cycle',
    },
    3: {
        'name': '概念集中度爆发',
        'description': 'TOP3概念涨停占比超过50%，主线明确，可积极参与',
        'category': 'concept',
    },
    4: {
        'name': '炸板率异常飙升',
        'description': '炸板率超过35%，封板失败率极高，短期回避',
        'category': 'risk',
    },
    5: {
        'name': '连板梯队断层',
        'description': '2板以上无股，仅首板活跃，梯队断档',
        'category': 'structure',
    },
    6: {
        'name': '情绪冰点反转',
        'description': '涨停数从低位(30以下)大幅回升(60以上)',
        'category': 'sentiment',
    },
    7: {
        'name': '龙头加速信号',
        'description': '最高板个股封板时间提前且封单比增大，龙头加速中',
        'category': 'dragon',
    },
    8: {
        'name': '高低切换信号',
        'description': '高位股开板增多，低位首板数量激增，风格可能切换',
        'category': 'rotation',
    },
}


# ─────────────────────────── 数据库工具 ───────────────────────────

def get_conn(db_path: str = DB_PATH) -> sqlite3.Connection:
    """获取数据库连接"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_tables(db_path: str = DB_PATH):
    """确保所需表存在"""
    conn = get_conn(db_path)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS recommendation_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rec_date TEXT NOT NULL,
        target_date TEXT NOT NULL,
        code TEXT NOT NULL,
        name TEXT,
        score REAL,
        reason TEXT,
        win_rate_estimate REAL,
        suggested_action TEXT,
        actual_result TEXT,
        actual_return REAL,
        is_correct INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS signal_tracking (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id INTEGER NOT NULL,
        trigger_date TEXT NOT NULL,
        trigger_stocks TEXT,
        next_day_result TEXT,
        avg_return REAL,
        verified INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS daily_tracking_report (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        report_date TEXT NOT NULL UNIQUE,
        market_summary TEXT,
        recommendation_performance TEXT,
        signal_status TEXT,
        next_day_advice TEXT,
        cumulative_win_rate REAL,
        total_recommendations INTEGER,
        correct_recommendations INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    conn.commit()
    conn.close()
    logger.info("实盘跟踪系统表初始化完成")


# ─────────────────────────── 数据查询辅助 ───────────────────────────

def get_latest_date(table: str = 'xgt_limit_up_detail',
                    db_path: str = DB_PATH) -> Optional[str]:
    """获取指定表最新日期"""
    conn = get_conn(db_path)
    try:
        row = conn.execute(f"SELECT MAX(date) as d FROM {table}").fetchone()
        return row['d'] if row and row['d'] else None
    finally:
        conn.close()


def get_next_trading_date(date: str, db_path: str = DB_PATH) -> Optional[str]:
    """获取指定日期的下一个交易日"""
    conn = get_conn(db_path)
    try:
        row = conn.execute("""
            SELECT MIN(date) as d FROM xgt_limit_up_detail WHERE date > ?
        """, (date,)).fetchone()
        return row['d'] if row and row['d'] else None
    finally:
        conn.close()


def get_prev_trading_date(date: str, db_path: str = DB_PATH) -> Optional[str]:
    """获取指定日期的上一个交易日"""
    conn = get_conn(db_path)
    try:
        row = conn.execute("""
            SELECT MAX(date) as d FROM xgt_limit_up_detail WHERE date < ?
        """, (date,)).fetchone()
        return row['d'] if row and row['d'] else None
    finally:
        conn.close()


def check_stock_limit_up_next_day(code: str, date: str,
                                   db_path: str = DB_PATH) -> Dict:
    """
    检查指定股票在指定日期的次日是否涨停
    
    Returns:
        {'next_date': str, 'is_limit_up': bool, 'change_percent': float, 'reason': str}
    """
    next_date = get_next_trading_date(date, db_path)
    if not next_date:
        return {'next_date': None, 'is_limit_up': False,
                'change_percent': 0, 'reason': '无下一交易日数据'}

    conn = get_conn(db_path)
    try:
        row = conn.execute("""
            SELECT code, name, change_percent, limit_up_days, concept, reason
            FROM xgt_limit_up_detail
            WHERE date = ? AND code = ?
        """, (next_date, code)).fetchone()

        if row:
            return {
                'next_date': next_date,
                'is_limit_up': True,
                'change_percent': row['change_percent'],
                'limit_up_days': row['limit_up_days'],
                'concept': row['concept'],
                'reason': row['reason'],
            }

        # 检查是否在炸板池
        break_row = conn.execute("""
            SELECT code, change_percent FROM xgt_break_limit_up
            WHERE date = ? AND code = ?
        """, (next_date, code)).fetchone()

        if break_row:
            return {
                'next_date': next_date,
                'is_limit_up': False,
                'change_percent': break_row['change_percent'],
                'reason': '次日炸板',
            }

        return {
            'next_date': next_date,
            'is_limit_up': False,
            'change_percent': 0,
            'reason': '次日未涨停',
        }
    finally:
        conn.close()


# ─────────────────────────── 每日跟踪 ───────────────────────────

def track_daily(date: str, db_path: str = DB_PATH) -> Dict[str, Any]:
    """
    每日跟踪：
    1. 获取当日推荐（recommendation_log 中 rec_date = date 的记录）
    2. 检查每只推荐股票在次日的实际表现
    3. 更新推荐记录的 actual_result, actual_return, is_correct 字段
    4. 计算累计胜率
    
    Returns:
        {
            'date': str,
            'recommendations_count': int,
            'correct_count': int,
            'win_rate': float,
            'details': List,
            'cumulative_win_rate': float,
        }
    """
    init_tables(db_path)
    conn = get_conn(db_path)

    try:
        # 获取当日推荐
        recs = conn.execute("""
            SELECT id, code, name, score, win_rate_estimate, suggested_action
            FROM recommendation_log
            WHERE rec_date = ?
        """, (date,)).fetchall()

        if not recs:
            logger.warning(f"[{date}] 没有找到推荐记录")
            return {'date': date, 'recommendations_count': 0, 'correct_count': 0,
                    'win_rate': 0, 'details': [], 'cumulative_win_rate': 0}

        details = []
        correct_count = 0

        for rec in recs:
            code = rec['code']
            result = check_stock_limit_up_next_day(code, date, db_path)

            is_correct = 1 if result['is_limit_up'] else 0
            actual_return = result.get('change_percent', 0) or 0
            actual_result_str = result.get('reason', '未知')

            if is_correct:
                correct_count += 1

            # 更新推荐记录
            next_d = result.get('next_date') or ''
            conn.execute("""
                UPDATE recommendation_log
                SET target_date = ?, actual_result = ?, actual_return = ?, is_correct = ?
                WHERE id = ?
            """, (next_d, actual_result_str, actual_return, is_correct, rec['id']))

            details.append({
                'code': code,
                'name': rec['name'],
                'score': rec['score'],
                'suggested_action': rec['suggested_action'],
                'is_limit_up_next_day': result['is_limit_up'],
                'actual_return': actual_return,
                'is_correct': is_correct,
                'next_date': result.get('next_date'),
                'result_desc': actual_result_str,
            })

        conn.commit()

        # 计算累计胜率
        all_recs = conn.execute("""
            SELECT is_correct FROM recommendation_log
            WHERE is_correct IS NOT NULL
        """).fetchall()
        total_verified = len(all_recs)
        total_correct = sum(1 for r in all_recs if r['is_correct'] == 1)
        cumulative_rate = total_correct / total_verified if total_verified > 0 else 0

        # 当日胜率
        win_rate = correct_count / len(recs) if recs else 0

        logger.info(f"[{date}] 跟踪完成: 推荐{len(recs)}只, "
                    f"命中{correct_count}只, 当日胜率{win_rate:.1%}, "
                    f"累计胜率{cumulative_rate:.1%}")

        return {
            'date': date,
            'recommendations_count': len(recs),
            'correct_count': correct_count,
            'win_rate': win_rate,
            'details': details,
            'cumulative_win_rate': cumulative_rate,
            'total_verified': total_verified,
            'total_correct': total_correct,
        }

    finally:
        conn.close()


# ─────────────────────────── 信号验证 ───────────────────────────

def _detect_signal_1(date: str, db_path: str) -> Tuple[bool, List[str]]:
    """信号1: 龙头断板反转 - 前一天的最高连板股次日是否断板"""
    prev_date = get_prev_trading_date(date, db_path)
    if not prev_date:
        return False, []

    conn = get_conn(db_path)
    try:
        # 找到前一日最高连板
        row = conn.execute("""
            SELECT code, name, limit_up_days FROM xgt_limit_up_detail
            WHERE date = ? ORDER BY limit_up_days DESC LIMIT 1
        """, (prev_date,)).fetchone()
        if not row or row['limit_up_days'] < 3:
            return False, []

        # 检查该股当日是否继续涨停
        curr = conn.execute("""
            SELECT code FROM xgt_limit_up_detail WHERE date = ? AND code = ?
        """, (date, row['code'])).fetchone()
        if not curr:
            return True, [f"{row['name']}({row['code']}){row['limit_up_days']}连板后断板"]
        return False, []
    finally:
        conn.close()


def _detect_signal_2(date: str, db_path: str) -> Tuple[bool, List[str]]:
    """信号2: 砸盘系数骤降 - 单日下降超过3.0"""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("""
            SELECT trade_date, smash_coefficient FROM smash_coefficients
            WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT 2
        """, (date,)).fetchall()
        if len(rows) < 2:
            return False, []
        curr = rows[0]['smash_coefficient']
        prev = rows[1]['smash_coefficient']
        if prev - curr > 3.0:
            return True, [f"砸盘系数从{prev:.1f}降至{curr:.1f}(降{prev-curr:.1f})"]
        return False, []
    finally:
        conn.close()


def _detect_signal_3(date: str, db_path: str) -> Tuple[bool, List[str]]:
    """信号3: 概念集中度爆发 - TOP3概念涨停占比超过50%"""
    conn = get_conn(db_path)
    try:
        # 总涨停数
        row = conn.execute("""
            SELECT limit_up_count FROM xgt_daily_summary WHERE date = ?
        """, (date,)).fetchone()
        total = row['limit_up_count'] if row else 0
        if total <= 0:
            return False, []

        # TOP3概念涨停数
        rows = conn.execute("""
            SELECT concept, count FROM concept_statistics
            WHERE date = ? ORDER BY count DESC LIMIT 3
        """, (date,)).fetchall()
        top3_total = sum(r['count'] for r in rows)
        ratio = top3_total / total if total > 0 else 0
        if ratio > 0.50:
            names = [r['concept'] for r in rows]
            return True, [f"TOP3概念({','.join(names)})占涨停总数{ratio:.0%}"]
        return False, []
    finally:
        conn.close()


def _detect_signal_4(date: str, db_path: str) -> Tuple[bool, List[str]]:
    """信号4: 炸板率异常飙升 - 超过35%"""
    conn = get_conn(db_path)
    try:
        row = conn.execute("""
            SELECT explosion_rate FROM xgt_daily_summary WHERE date = ?
        """, (date,)).fetchone()
        if row and row['explosion_rate'] and row['explosion_rate'] > 0.35:
            return True, [f"炸板率{row['explosion_rate']:.0%}，超过35%警戒线"]
        return False, []
    finally:
        conn.close()


def _detect_signal_5(date: str, db_path: str) -> Tuple[bool, List[str]]:
    """信号5: 连板梯队断层 - 2板以上无股"""
    conn = get_conn(db_path)
    try:
        row = conn.execute("""
            SELECT MAX(limit_up_days) as max_days FROM xgt_limit_up_detail
            WHERE date = ?
        """, (date,)).fetchone()
        if row and row['max_days'] is not None:
            max_days = row['max_days']
            if max_days <= 1:
                # 查首板数量
                count = conn.execute("""
                    SELECT COUNT(*) as cnt FROM xgt_limit_up_detail
                    WHERE date = ? AND limit_up_days = 1
                """, (date,)).fetchone()
                cnt = count['cnt'] if count else 0
                return True, [f"最高板仅{max_days}板，梯队断层，首板{cnt}只"]
        return False, []
    finally:
        conn.close()


def _detect_signal_6(date: str, db_path: str) -> Tuple[bool, List[str]]:
    """信号6: 情绪冰点反转 - 涨停数从30以下回升到60以上"""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("""
            SELECT date, limit_up_count FROM xgt_daily_summary
            WHERE date <= ? ORDER BY date DESC LIMIT 2
        """, (date,)).fetchall()
        if len(rows) >= 2:
            curr = rows[0]['limit_up_count']
            prev = rows[1]['limit_up_count']
            if prev < 30 and curr >= 60:
                return True, [f"涨停数从前日{prev}回升至今日{curr}"]
        return False, []
    finally:
        conn.close()


def _detect_signal_7(date: str, db_path: str) -> Tuple[bool, List[str]]:
    """信号7: 龙头加速 - 最高板股封板时间提前且封单比增大"""
    conn = get_conn(db_path)
    try:
        # 找到当日最高板股
        curr_top = conn.execute("""
            SELECT code, name, limit_up_days, first_limit_up_time, seal_ratio
            FROM xgt_limit_up_detail WHERE date = ?
            ORDER BY limit_up_days DESC, seal_ratio DESC LIMIT 1
        """, (date,)).fetchone()
        if not curr_top or curr_top['limit_up_days'] < 2:
            return False, []

        prev_date = get_prev_trading_date(date, db_path)
        if not prev_date:
            return False, []

        prev_top = conn.execute("""
            SELECT code, name, first_limit_up_time, seal_ratio
            FROM xgt_limit_up_detail WHERE date = ? AND code = ?
        """, (prev_date, curr_top['code'])).fetchone()
        if not prev_top:
            return False, []

        # 比较封板时间（简化为字符串比较）
        curr_time = curr_top['first_limit_up_time'] or '15:00'
        prev_time = prev_top['first_limit_up_time'] or '15:00'
        curr_seal = curr_top['seal_ratio'] or 0
        prev_seal = prev_top['seal_ratio'] or 0

        if curr_time < prev_time and curr_seal > prev_seal:
            return True, [f"{curr_top['name']}封板时间从{prev_time}提前到{curr_time},"
                         f"封单比从{prev_seal:.2%}增到{curr_seal:.2%}"]
        return False, []
    finally:
        conn.close()


def _detect_signal_8(date: str, db_path: str) -> Tuple[bool, List[str]]:
    """信号8: 高低切换 - 高位股开板增多，低位首板激增"""
    conn = get_conn(db_path)
    try:
        # 当日首板数和3板以上数
        curr_first = conn.execute("""
            SELECT COUNT(*) as cnt FROM xgt_limit_up_detail
            WHERE date = ? AND limit_up_days = 1
        """, (date,)).fetchone()
        curr_high = conn.execute("""
            SELECT COUNT(*) as cnt FROM xgt_limit_up_detail
            WHERE date = ? AND limit_up_days >= 3
        """, (date,)).fetchone()

        prev_date = get_prev_trading_date(date, db_path)
        if not prev_date:
            return False, []

        prev_first = conn.execute("""
            SELECT COUNT(*) as cnt FROM xgt_limit_up_detail
            WHERE date = ? AND limit_up_days = 1
        """, (prev_date,)).fetchone()
        prev_high = conn.execute("""
            SELECT COUNT(*) as cnt FROM xgt_limit_up_detail
            WHERE date = ? AND limit_up_days >= 3
        """, (prev_date,)).fetchone()

        cf = curr_first['cnt'] if curr_first else 0
        ch = curr_high['cnt'] if curr_high else 0
        pf = prev_first['cnt'] if prev_first else 0
        ph = prev_high['cnt'] if prev_high else 0

        # 首板增加50%以上 且 高位股减少50%以上
        if pf > 0 and cf > pf * 1.5 and ph > 0 and ch < ph * 0.5:
            return True, [f"首板{pf}→{cf}(+{(cf-pf)/pf:.0%}),"
                         f"3板以上{ph}→{ch}(-{(ph-ch)/ph:.0%})"]
        return False, []
    finally:
        conn.close()


# 信号检测函数映射
SIGNAL_DETECTORS = {
    1: _detect_signal_1,
    2: _detect_signal_2,
    3: _detect_signal_3,
    4: _detect_signal_4,
    5: _detect_signal_5,
    6: _detect_signal_6,
    7: _detect_signal_7,
    8: _detect_signal_8,
}


def evaluate_signals(date: str, db_path: str = DB_PATH) -> Dict[str, Any]:
    """
    信号验证：检查所有8个信号是否在指定日期触发，并跟踪触发后的次日表现
    
    Returns:
        {
            'date': str,
            'signals_triggered': List[Dict],  # 触发的信号
            'signals_not_triggered': List[int], # 未触发的信号ID
            'newly_verified': List[Dict],  # 新完成验证的信号
        }
    """
    init_tables(db_path)
    conn = get_conn(db_path)

    triggered = []
    not_triggered = []

    try:
        # 检测当日触发的信号
        for sig_id, detector in SIGNAL_DETECTORS.items():
            is_triggered, details = detector(date, db_path)
            sig_def = SIGNAL_DEFINITIONS.get(sig_id, {})

            if is_triggered:
                # 检查是否已记录
                existing = conn.execute("""
                    SELECT id FROM signal_tracking
                    WHERE signal_id = ? AND trigger_date = ?
                """, (sig_id, date)).fetchone()

                if not existing:
                    conn.execute("""
                        INSERT INTO signal_tracking (signal_id, trigger_date, trigger_stocks)
                        VALUES (?, ?, ?)
                    """, (sig_id, date, json.dumps(details, ensure_ascii=False)))
                    logger.info(f"[{date}] 信号{sig_id}触发: {sig_def.get('name', '')} - {details}")

                triggered.append({
                    'signal_id': sig_id,
                    'name': sig_def.get('name', ''),
                    'description': sig_def.get('description', ''),
                    'details': details,
                })
            else:
                not_triggered.append(sig_id)

        conn.commit()

        # 验证之前触发的信号（检查次日表现）
        newly_verified = []
        # 找到所有未验证的触发记录
        unverified = conn.execute("""
            SELECT id, signal_id, trigger_date, trigger_stocks
            FROM signal_tracking WHERE verified = 0
        """).fetchall()

        for uv in unverified:
            next_date = get_next_trading_date(uv['trigger_date'], db_path)
            if not next_date or next_date > date:
                continue  # 还没有次日数据

            # 获取次日市场概况
            next_summary = conn.execute("""
                SELECT limit_up_count, explosion_rate, max_continuous_boards
                FROM xgt_daily_summary WHERE date = ?
            """, (next_date,)).fetchone()

            if next_summary:
                result_data = {
                    'limit_up_count': next_summary['limit_up_count'],
                    'explosion_rate': next_summary['explosion_rate'],
                    'max_boards': next_summary['max_continuous_boards'],
                }
                # 简单计算次日收益表现（用涨停数变化估算）
                curr_summary = conn.execute("""
                    SELECT limit_up_count FROM xgt_daily_summary WHERE date = ?
                """, (uv['trigger_date'],)).fetchone()
                if curr_summary and curr_summary['limit_up_count']:
                    avg_return = ((next_summary['limit_up_count'] or 0) -
                                  curr_summary['limit_up_count']) / max(curr_summary['limit_up_count'], 1) * 100
                else:
                    avg_return = 0

                conn.execute("""
                    UPDATE signal_tracking
                    SET next_day_result = ?, avg_return = ?, verified = 1
                    WHERE id = ?
                """, (json.dumps(result_data, ensure_ascii=False), avg_return, uv['id']))

                newly_verified.append({
                    'signal_id': uv['signal_id'],
                    'trigger_date': uv['trigger_date'],
                    'next_date': next_date,
                    'avg_return': avg_return,
                })

        conn.commit()

        return {
            'date': date,
            'signals_triggered': triggered,
            'signals_not_triggered': not_triggered,
            'newly_verified': newly_verified,
        }

    finally:
        conn.close()


# ─────────────────────────── 累计统计 ───────────────────────────

def get_cumulative_stats(db_path: str = DB_PATH) -> Dict[str, Any]:
    """获取累计推荐统计"""
    conn = get_conn(db_path)
    try:
        total = conn.execute("""
            SELECT COUNT(*) as cnt FROM recommendation_log WHERE is_correct IS NOT NULL
        """).fetchone()
        correct = conn.execute("""
            SELECT COUNT(*) as cnt FROM recommendation_log WHERE is_correct = 1
        """).fetchone()

        # 按维度统计准确率
        by_score_range = conn.execute("""
            SELECT
                CASE
                    WHEN score >= 80 THEN '高分(80+)'
                    WHEN score >= 65 THEN '中高分(65-80)'
                    WHEN score >= 50 THEN '中分(50-65)'
                    ELSE '低分(<50)'
                END as score_range,
                COUNT(*) as total,
                SUM(is_correct) as correct
            FROM recommendation_log
            WHERE is_correct IS NOT NULL
            GROUP BY score_range
        """).fetchall()

        # 信号胜率
        signal_stats = conn.execute("""
            SELECT signal_id, COUNT(*) as total_triggers,
                   AVG(avg_return) as avg_return
            FROM signal_tracking WHERE verified = 1
            GROUP BY signal_id
        """).fetchall()

        return {
            'total_recommendations': total['cnt'] if total else 0,
            'total_correct': correct['cnt'] if correct else 0,
            'cumulative_win_rate': (correct['cnt'] / total['cnt'] if total and total['cnt'] > 0 else 0),
            'by_score_range': [dict(r) for r in by_score_range],
            'signal_stats': [dict(r) for r in signal_stats],
        }
    finally:
        conn.close()


# ─────────────────────────── 每日报告 ───────────────────────────

def generate_daily_report(date: str, db_path: str = DB_PATH) -> str:
    """
    生成每日跟踪报告
    
    Returns:
        报告文本（同时保存到 reports/ 目录）
    """
    init_tables(db_path)

    # 1. 跟踪当日推荐表现
    tracking = track_daily(date, db_path)

    # 2. 信号验证
    signals = evaluate_signals(date, db_path)

    # 3. 市场概况
    conn = get_conn(db_path)
    try:
        summary = conn.execute("""
            SELECT * FROM xgt_daily_summary WHERE date = ?
        """, (date,)).fetchone()
        summary_dict = dict(summary) if summary else {}
    finally:
        conn.close()

    # 4. 累计统计
    cum_stats = get_cumulative_stats(db_path)

    # 5. 构建报告
    lines = []
    lines.append(f"{'='*65}")
    lines.append(f"  📋 每日跟踪报告 | {date}")
    lines.append(f"{'='*65}")
    lines.append("")

    # 市场概况
    lines.append("📊 今日市场概况:")
    lines.append(f"  涨停数: {summary_dict.get('limit_up_count', 'N/A')}")
    lines.append(f"  跌停数: {summary_dict.get('limit_down_count', 'N/A')}")
    lines.append(f"  炸板数: {summary_dict.get('break_limit_up_count', 'N/A')}")
    lines.append(f"  炸板率: {summary_dict.get('explosion_rate', 0):.1%}" if summary_dict.get('explosion_rate') else "  炸板率: N/A")
    lines.append(f"  最高板: {summary_dict.get('max_continuous_boards', 'N/A')}")
    lu = summary_dict.get('limit_up_count', 0) or 0
    ld = summary_dict.get('limit_down_count', 0) or 0
    rc = summary_dict.get('rise_count', 0) or 0
    fc = summary_dict.get('fall_count', 0) or 0
    lines.append(f"  涨跌比: {rc}:{fc}")
    lines.append("")

    # 推荐表现
    lines.append("🎯 推荐表现回顾:")
    if tracking['recommendations_count'] > 0:
        lines.append(f"  推荐数: {tracking['recommendations_count']}")
        lines.append(f"  命中数: {tracking['correct_count']}")
        lines.append(f"  当日胜率: {tracking['win_rate']:.1%}")
        lines.append("")
        for d in tracking.get('details', []):
            mark = '✅' if d['is_correct'] else '❌'
            lines.append(f"  {mark} {d['name']}({d['code']}) "
                        f"评分{d['score']} → {d['suggested_action']}")
            lines.append(f"     次日: {d['result_desc']} "
                        f"({'涨' if d['actual_return'] > 0 else '跌'}{abs(d['actual_return']):.1%})")
    else:
        lines.append("  当日无推荐记录")
    lines.append("")

    # 信号触发
    lines.append("📡 信号触发情况:")
    if signals['signals_triggered']:
        for sig in signals['signals_triggered']:
            lines.append(f"  🔔 信号{sig['signal_id']}「{sig['name']}」已触发")
            for detail in sig.get('details', []):
                lines.append(f"     {detail}")
    else:
        lines.append("  今日无信号触发")

    if signals.get('newly_verified'):
        lines.append("")
        lines.append("  已验证信号:")
        for v in signals['newly_verified']:
            sig_name = SIGNAL_DEFINITIONS.get(v['signal_id'], {}).get('name', '')
            lines.append(f"    信号{v['signal_id']}「{sig_name}」"
                        f"触发于{v['trigger_date']} → 次日收益{v['avg_return']:.1f}%")
    lines.append("")

    # 累计统计
    lines.append("📈 累计表现:")
    lines.append(f"  总推荐: {cum_stats['total_recommendations']}只")
    lines.append(f"  总命中: {cum_stats['total_correct']}只")
    lines.append(f"  累计胜率: {cum_stats['cumulative_win_rate']:.1%}")
    if cum_stats.get('by_score_range'):
        lines.append("  分段胜率:")
        for sr in cum_stats['by_score_range']:
            total = sr['total']
            correct = sr['correct'] or 0
            rate = correct / total if total > 0 else 0
            lines.append(f"    {sr['score_range']}: {correct}/{total} ({rate:.0%})")
    lines.append("")

    lines.append(f"{'='*65}")
    lines.append("⚠️ 以上为AI模型跟踪分析结果，仅供参考，不构成投资建议。")
    lines.append(f"{'='*65}")

    report_text = '\n'.join(lines)

    # 保存报告到文件
    _save_report(date, report_text)

    # 同时保存到数据库
    _save_report_to_db(date, summary_dict, tracking, signals, cum_stats, db_path)

    return report_text


def _save_report(date: str, report_text: str):
    """保存报告到文件"""
    os.makedirs(REPORTS_DIR, exist_ok=True)
    filepath = os.path.join(REPORTS_DIR, f"report_{date}.txt")
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(report_text)
        logger.info(f"报告已保存: {filepath}")
    except Exception as e:
        logger.error(f"保存报告失败: {e}")


def _save_report_to_db(date: str, summary: Dict, tracking: Dict,
                        signals: Dict, cum_stats: Dict, db_path: str):
    """保存报告摘要到数据库"""
    conn = get_conn(db_path)
    try:
        conn.execute("""
            INSERT OR REPLACE INTO daily_tracking_report
            (report_date, market_summary, recommendation_performance,
             signal_status, next_day_advice, cumulative_win_rate,
             total_recommendations, correct_recommendations)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            date,
            json.dumps(summary, ensure_ascii=False, default=str),
            json.dumps({
                'count': tracking['recommendations_count'],
                'correct': tracking['correct_count'],
                'win_rate': tracking['win_rate'],
            }, ensure_ascii=False),
            json.dumps({
                'triggered': [s['signal_id'] for s in signals.get('signals_triggered', [])],
                'verified': len(signals.get('newly_verified', [])),
            }, ensure_ascii=False),
            '',  # next_day_advice 可由 recommender 填充
            cum_stats['cumulative_win_rate'],
            cum_stats['total_recommendations'],
            cum_stats['total_correct'],
        ))
        conn.commit()
    finally:
        conn.close()


# ─────────────────────────── 主程序 ───────────────────────────

def main():
    parser = argparse.ArgumentParser(description='实盘跟踪系统')
    parser.add_argument('--date', type=str, default=None,
                        help='指定日期 (YYYY-MM-DD)，默认使用最新交易日')
    parser.add_argument('--report', action='store_true',
                        help='生成完整报告')
    parser.add_argument('--signals-only', action='store_true',
                        help='仅检查信号')
    parser.add_argument('--stats', action='store_true',
                        help='查看累计统计')
    parser.add_argument('--db', type=str, default=DB_PATH,
                        help='数据库路径')
    args = parser.parse_args()

    db_path = args.db
    init_tables(db_path)

    # 确定日期
    if args.date:
        date = args.date
    else:
        date = get_latest_date('xgt_limit_up_detail', db_path)
        if not date:
            logger.error("无法获取最新交易日")
            sys.exit(1)
        logger.info(f"使用最新交易日: {date}")

    if args.stats:
        # 累计统计模式
        cum = get_cumulative_stats(db_path)
        print(f"\n📈 累计推荐统计:")
        print(f"  总推荐: {cum['total_recommendations']}只")
        print(f"  总命中: {cum['total_correct']}只")
        print(f"  累计胜率: {cum['cumulative_win_rate']:.1%}")
        if cum.get('by_score_range'):
            print(f"\n  分段准确率:")
            for sr in cum['by_score_range']:
                total = sr['total']
                correct = sr['correct'] or 0
                rate = correct / total if total > 0 else 0
                print(f"    {sr['score_range']}: {correct}/{total} ({rate:.0%})")
        if cum.get('signal_stats'):
            print(f"\n  信号表现:")
            for ss in cum['signal_stats']:
                sig_name = SIGNAL_DEFINITIONS.get(ss['signal_id'], {}).get('name', '')
                print(f"    信号{ss['signal_id']}「{sig_name}」: "
                      f"触发{ss['total_triggers']}次, "
                      f"平均收益{ss['avg_return'] or 0:.1f}%")
        return

    if args.signals_only:
        # 仅信号模式
        signals = evaluate_signals(date, db_path)
        print(f"\n📡 [{date}] 信号检测:")
        if signals['signals_triggered']:
            for sig in signals['signals_triggered']:
                print(f"  🔔 信号{sig['signal_id']}「{sig['name']}」已触发")
                for d in sig.get('details', []):
                    print(f"     {d}")
        else:
            print("  无信号触发")
        return

    if args.report:
        # 完整报告模式
        report = generate_daily_report(date, db_path)
        print(report)
    else:
        # 默认：仅跟踪
        tracking = track_daily(date, db_path)
        print(f"\n📊 [{date}] 跟踪结果:")
        print(f"  推荐: {tracking['recommendations_count']}只")
        print(f"  命中: {tracking['correct_count']}只")
        print(f"  胜率: {tracking['win_rate']:.1%}")
        print(f"  累计: {tracking.get('cumulative_win_rate', 0):.1%}")
        if tracking.get('details'):
            print(f"\n  详情:")
            for d in tracking['details']:
                mark = '✅' if d['is_correct'] else '❌'
                print(f"  {mark} {d['name']}({d['code']}) → {d['result_desc']}")


if __name__ == '__main__':
    main()
