#!/usr/bin/env python3
"""
hotfix6.py - 数据补全修复脚本
功能：
1. 计算所有交易日的砸盘系数并写入 smash_coefficient_results 和 smash_coefficients 表
2. 重新运行信号检测，回填 signal_tracking 表
3. 修复推荐引擎的砸盘系数显示问题

运行方式：将此文件放到 market_advisor 目录下，执行 python hotfix6.py
"""
import os
import sys
import json
import sqlite3
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# 自动检测数据库路径
def find_db():
    """在当前目录及父目录中查找数据库文件"""
    for pattern in ['stock_data_*.db', '*.db']:
        import glob
        for f in glob.glob(pattern):
            if 'stock_data' in f:
                return f
    # 尝试常见路径
    candidates = [
        os.path.join(os.path.dirname(__file__), 'stock_data.db'),
        os.path.join(os.path.dirname(os.path.dirname(__file__)), 'stock_data.db'),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None

def compute_smash_coefficients(db_path):
    """计算所有交易日的砸盘系数"""
    logger.info("=" * 60)
    logger.info("步骤1: 计算砸盘系数")
    logger.info("=" * 60)
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    # 获取所有有涨停数据的日期
    dates = conn.execute("""
        SELECT DISTINCT date FROM xgt_limit_up_detail ORDER BY date
    """).fetchall()
    dates = [r['date'] for r in dates]
    logger.info(f"共有 {len(dates)} 个交易日的涨停数据: {dates[0]} ~ {dates[-1]}")
    
    # 获取每日板级分布
    daily_boards = {}
    for date in dates:
        rows = conn.execute("""
            SELECT limit_up_days, COUNT(*) as cnt 
            FROM xgt_limit_up_detail WHERE date = ?
            GROUP BY limit_up_days
        """, (date,)).fetchall()
        daily_boards[date] = {r['limit_up_days']: r['cnt'] for r in rows}
    
    # 计算砸盘系数
    results = []
    for i in range(1, len(dates)):
        date = dates[i]
        prev_date = dates[i-1]
        
        today_boards = daily_boards[date]
        prev_boards = daily_boards[prev_date]
        
        ratios = []
        max_board_today = max(today_boards.keys()) if today_boards else 0
        
        for n in range(2, max_board_today + 1):
            today_n = today_boards.get(n, 0)
            prev_n_minus_1 = prev_boards.get(n - 1, 0)
            if prev_n_minus_1 > 0 and today_n > 0:
                ratio = today_n / prev_n_minus_1
                ratios.append(ratio)
        
        if ratios:
            smash_coeff = round(sum(ratios) / len(ratios) * 10, 2)
            results.append({
                'date': date,
                'smash_coefficient': smash_coeff,
                'max_boards': max_board_today,
            })
    
    # 确保表存在
    conn.execute("""
        CREATE TABLE IF NOT EXISTS smash_coefficient_results (
            date TEXT PRIMARY KEY,
            smash_coefficient REAL,
            max_continuous_boards INTEGER,
            created_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS smash_coefficients (
            trade_date TEXT PRIMARY KEY,
            smash_coefficient REAL,
            limit_up_count INTEGER,
            avg_continuous_days REAL,
            max_continuous_days INTEGER,
            open_rate REAL,
            update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # 写入
    count = 0
    for r in results:
        conn.execute("""
            INSERT OR REPLACE INTO smash_coefficient_results 
            (date, smash_coefficient, max_continuous_boards)
            VALUES (?, ?, ?)
        """, (r['date'], r['smash_coefficient'], r['max_boards']))
        
        # 获取当日涨停数
        limit_up_row = conn.execute(
            "SELECT limit_up_count FROM xgt_daily_summary WHERE date = ?", 
            (r['date'],)
        ).fetchone()
        luc = limit_up_row['limit_up_count'] if limit_up_row else 0
        
        conn.execute("""
            INSERT OR REPLACE INTO smash_coefficients 
            (trade_date, smash_coefficient, limit_up_count, max_continuous_days)
            VALUES (?, ?, ?, ?)
        """, (r['date'], r['smash_coefficient'], luc, r['max_boards']))
        count += 1
    
    conn.commit()
    logger.info(f"✅ 砸盘系数计算完成: {count} 条记录已写入")
    
    # 显示最近数据
    cursor = conn.execute("SELECT trade_date, smash_coefficient, limit_up_count, max_continuous_days FROM smash_coefficients ORDER BY trade_date DESC LIMIT 5")
    for r in cursor.fetchall():
        logger.info(f"  {r['trade_date']}: 砸盘={r['smash_coefficient']}, 涨停={r['limit_up_count']}, 最高{r['max_continuous_days']}板")
    
    conn.close()
    return results


def backfill_signals(db_path):
    """回填信号检测数据"""
    logger.info("")
    logger.info("=" * 60)
    logger.info("步骤2: 回填信号检测数据")
    logger.info("=" * 60)
    
    # 尝试导入 live_tracker
    try:
        sys.path.insert(0, os.path.dirname(__file__))
        import importlib
        
        # 尝试多个路径
        live_tracker = None
        for module_name in ['live_tracker', 'market_advisor_upgrade.live_tracker']:
            try:
                mod = importlib.import_module(module_name)
                if hasattr(mod, 'evaluate_signals'):
                    live_tracker = mod
                    break
            except ImportError:
                continue
        
        if not live_tracker:
            logger.warning("⚠️  无法导入 live_tracker 模块，跳过信号回填")
            return
        
        # 初始化表
        live_tracker.init_tables(db_path)
        
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        
        # 获取所有有数据的日期
        dates = conn.execute("""
            SELECT DISTINCT date FROM xgt_limit_up_detail ORDER BY date
        """).fetchall()
        dates = [r['date'] for r in dates]
        
        # 清除旧的自动生成记录，重新检测
        conn.execute("DELETE FROM signal_tracking")
        conn.commit()
        conn.close()
        
        total_triggered = 0
        for date in dates:
            result = live_tracker.evaluate_signals(date, db_path)
            triggered = result['signals_triggered']
            if triggered:
                total_triggered += len(triggered)
        
        logger.info(f"✅ 信号回填完成: {len(dates)} 天检测, 共触发 {total_triggered} 条信号记录")
        
        # 显示最近的信号
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("""
            SELECT trigger_date, signal_id, trigger_stocks 
            FROM signal_tracking ORDER BY trigger_date DESC LIMIT 10
        """)
        signal_names = {1:'龙头断板反转',2:'砸盘系数骤降',3:'概念集中度爆发',4:'炸板率飙升',
                       5:'连板梯队断层',6:'情绪冰点反转',7:'龙头加速',8:'高低切换'}
        for r in cursor.fetchall():
            name = signal_names.get(r['signal_id'], f"信号{r['signal_id']}")
            stocks = ''
            try:
                stocks = ', '.join(json.loads(r['trigger_stocks'] or '[]'))
            except:
                stocks = r['trigger_stocks'] or ''
            logger.info(f"  {r['trigger_date']}: {name} - {stocks}")
        conn.close()
        
    except Exception as e:
        logger.error(f"❌ 信号回填失败: {e}")
        import traceback
        traceback.print_exc()


def verify(db_path):
    """验证修复结果"""
    logger.info("")
    logger.info("=" * 60)
    logger.info("步骤3: 验证修复结果")
    logger.info("=" * 60)
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    # 砸盘系数
    cursor = conn.execute("SELECT COUNT(*) as cnt FROM smash_coefficients")
    cnt = cursor.fetchone()['cnt']
    logger.info(f"  smash_coefficients: {cnt} 条记录")
    
    cursor = conn.execute("SELECT COUNT(*) as cnt FROM smash_coefficient_results")
    cnt = cursor.fetchone()['cnt']
    logger.info(f"  smash_coefficient_results: {cnt} 条记录")
    
    # 信号
    cursor = conn.execute("SELECT COUNT(*) as cnt FROM signal_tracking")
    cnt = cursor.fetchone()['cnt']
    logger.info(f"  signal_tracking: {cnt} 条记录")
    
    # 最新日期数据
    cursor = conn.execute("SELECT MAX(date) as d FROM xgt_daily_summary")
    latest = cursor.fetchone()['d']
    logger.info(f"  最新数据日期: {latest}")
    
    cursor = conn.execute("SELECT MAX(trade_date) as d FROM smash_coefficients")
    latest_smash = cursor.fetchone()['d']
    logger.info(f"  最新砸盘系数日期: {latest_smash}")
    
    cursor = conn.execute("SELECT MAX(trigger_date) as d FROM signal_tracking")
    latest_signal = cursor.fetchone()['d']
    if latest_signal:
        logger.info(f"  最新信号日期: {latest_signal}")
    
    conn.close()
    logger.info("")
    logger.info("✅ 修复完成！请重启服务 (python app.py) 后刷新页面验证")


if __name__ == '__main__':
    db_path = find_db()
    if not db_path:
        logger.error("❌ 未找到数据库文件！请将此脚本放到 market_advisor 目录下运行")
        sys.exit(1)
    
    logger.info(f"数据库: {db_path}")
    logger.info("")
    
    compute_smash_coefficients(db_path)
    backfill_signals(db_path)
    verify(db_path)
