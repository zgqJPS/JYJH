"""
db_v3_patch.py - 数据库补丁模块
为market_advisor系统新增选股通相关数据表
"""

import sqlite3
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# 建表SQL
CREATE_TABLES_SQL = """
-- 选股通盯盘每日汇总
CREATE TABLE IF NOT EXISTS xgt_daily_summary (
    date TEXT PRIMARY KEY,
    limit_up_count INTEGER,        -- 涨停数
    limit_down_count INTEGER,      -- 跌停数
    break_limit_up_count INTEGER,  -- 炸板数
    rise_count INTEGER,            -- 上涨家数
    fall_count INTEGER,            -- 下跌家数
    explosion_rate REAL,           -- 炸板率
    rise_fall_ratio REAL,          -- 涨跌比
    yesterday_limit_up_avg_change REAL,  -- 昨日涨停今日表现(均涨幅)
    market_heat REAL,              -- 市场真实热度(0-100)
    max_continuous_boards INTEGER, -- 最高连板
    board_distribution TEXT,       -- 连板分布JSON {"1":n,"2":n,...}
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 选股通涨停池详情
CREATE TABLE IF NOT EXISTS xgt_limit_up_detail (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    price REAL,
    change_percent REAL,
    limit_up_days INTEGER DEFAULT 1,
    first_limit_up_time TEXT,
    last_limit_up_time TEXT,
    break_times INTEGER DEFAULT 0,
    seal_ratio REAL,
    turnover_rate REAL,
    volume_bias REAL,
    flow_capital REAL,
    total_capital REAL,
    concept TEXT,
    reason TEXT,
    UNIQUE(date, code)
);

-- 选股通炸板池
CREATE TABLE IF NOT EXISTS xgt_break_limit_up (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    change_percent REAL,
    limit_up_days INTEGER,
    break_times INTEGER,
    concept TEXT,
    UNIQUE(date, code)
);

-- 选股通跌停池
CREATE TABLE IF NOT EXISTS xgt_limit_down (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    change_percent REAL,
    break_times INTEGER,
    UNIQUE(date, code)
);
"""


def init_xgt_tables(db_path: str) -> bool:
    """
    初始化选股通相关数据表
    
    Args:
        db_path: 数据库文件路径
    
    Returns:
        是否成功
    """
    try:
        conn = sqlite3.connect(db_path, isolation_level=None)
        cursor = conn.cursor()
        
        # 执行所有建表语句
        cursor.executescript(CREATE_TABLES_SQL)
        
        # 验证表是否创建成功
        tables = ['xgt_daily_summary', 'xgt_limit_up_detail', 
                  'xgt_break_limit_up', 'xgt_limit_down']
        
        for table in tables:
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,)
            )
            if cursor.fetchone():
                logger.info(f"表 {table} 创建/已存在")
            else:
                logger.error(f"表 {table} 创建失败!")
                conn.close()
                return False
        
        conn.close()
        logger.info(f"选股通数据表初始化完成: {db_path}")
        return True
        
    except Exception as e:
        logger.error(f"初始化选股通数据表失败: {e}")
        return False


def insert_daily_summary(db_path: str, data: dict) -> bool:
    """
    插入每日汇总数据
    
    Args:
        db_path: 数据库路径
        data: 包含 date, metrics, market_indicators 的数据字典
    
    Returns:
        是否成功
    """
    import json
    
    try:
        conn = sqlite3.connect(db_path, isolation_level=None)
        cursor = conn.cursor()
        
        metrics = data.get('metrics', {})
        indicators = data.get('market_indicators', {})
        
        # 涨停数优先用池数据数量，其次用市场指标
        limit_up_count = len(data.get('pools', {}).get('limit_up', []))
        if indicators.get('limit_up_count') is not None:
            limit_up_count = max(limit_up_count, indicators['limit_up_count'])
        
        break_count = len(data.get('pools', {}).get('limit_up_broken', []))
        limit_down_count = len(data.get('pools', {}).get('limit_down', []))
        if indicators.get('limit_down_count') is not None:
            limit_down_count = max(limit_down_count, indicators['limit_down_count'])
        
        sql = """
        INSERT OR REPLACE INTO xgt_daily_summary 
        (date, limit_up_count, limit_down_count, break_limit_up_count,
         rise_count, fall_count, explosion_rate, rise_fall_ratio,
         yesterday_limit_up_avg_change, market_heat, max_continuous_boards,
         board_distribution)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        
        params = (
            data['date'],
            limit_up_count,
            limit_down_count,
            break_count,
            indicators.get('rise_count'),
            indicators.get('fall_count'),
            metrics.get('explosion_rate'),
            metrics.get('rise_fall_ratio'),
            metrics.get('yesterday_limit_up_avg_change'),
            metrics.get('market_heat'),
            metrics.get('max_continuous_boards'),
            json.dumps(metrics.get('board_distribution', {}), ensure_ascii=False)
        )
        
        cursor.execute(sql, params)
        conn.close()
        return True
        
    except Exception as e:
        logger.error(f"插入每日汇总数据失败: {e}")
        return False


def insert_limit_up_details(db_path: str, date: str, stocks: list) -> int:
    """
    插入涨停池详情数据
    
    Args:
        db_path: 数据库路径
        date: 日期
        stocks: 股票列表
    
    Returns:
        成功插入的记录数
    """
    try:
        conn = sqlite3.connect(db_path, isolation_level=None)
        cursor = conn.cursor()
        
        count = 0
        for stock in stocks:
            try:
                # 导入辅助函数
                import sys
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                from xuangutong_fetcher import parse_symbol, parse_surge_reason, format_timestamp
                
                code = parse_symbol(stock.get('symbol', ''))
                reason, concepts = parse_surge_reason(stock)
                
                # 流通市值和总市值转为亿
                flow_cap = stock.get('non_restricted_capital')
                if flow_cap is not None:
                    flow_cap = round(flow_cap / 1e8, 2)  # 转为亿
                
                total_cap = stock.get('total_capital')
                if total_cap is not None:
                    total_cap = round(total_cap / 1e8, 2)
                
                sql = """
                INSERT OR REPLACE INTO xgt_limit_up_detail
                (date, code, name, price, change_percent, limit_up_days,
                 first_limit_up_time, last_limit_up_time, break_times,
                 seal_ratio, turnover_rate, volume_bias, flow_capital,
                 total_capital, concept, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
                
                params = (
                    date,
                    code,
                    stock.get('stock_chi_name', ''),
                    stock.get('price'),
                    stock.get('change_percent'),
                    stock.get('limit_up_days', 1),
                    format_timestamp(stock.get('first_limit_up')),
                    format_timestamp(stock.get('last_limit_up')),
                    stock.get('break_limit_up_times', 0),
                    stock.get('buy_lock_volume_ratio'),
                    stock.get('turnover_ratio'),
                    stock.get('volume_bias_ratio'),
                    flow_cap,
                    total_cap,
                    concepts,
                    reason
                )
                
                cursor.execute(sql, params)
                count += 1
            except Exception as e:
                logger.warning(f"插入涨停详情单条记录失败: {stock.get('symbol', 'unknown')} - {e}")
                continue
        
        conn.close()
        return count
        
    except Exception as e:
        logger.error(f"插入涨停池详情数据失败: {e}")
        return 0


def insert_break_limit_up(db_path: str, date: str, stocks: list) -> int:
    """插入炸板池数据"""
    try:
        conn = sqlite3.connect(db_path, isolation_level=None)
        cursor = conn.cursor()
        
        count = 0
        for stock in stocks:
            try:
                import sys
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                from xuangutong_fetcher import parse_symbol, parse_surge_reason
                
                code = parse_symbol(stock.get('symbol', ''))
                _, concepts = parse_surge_reason(stock)
                
                sql = """
                INSERT OR REPLACE INTO xgt_break_limit_up
                (date, code, name, change_percent, limit_up_days, break_times, concept)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """
                
                params = (
                    date, code,
                    stock.get('stock_chi_name', ''),
                    stock.get('change_percent'),
                    stock.get('limit_up_days'),
                    stock.get('break_limit_up_times', 0),
                    concepts
                )
                
                cursor.execute(sql, params)
                count += 1
            except Exception as e:
                logger.warning(f"插入炸板池单条记录失败: {e}")
                continue
        
        conn.close()
        return count
        
    except Exception as e:
        logger.error(f"插入炸板池数据失败: {e}")
        return 0


def insert_limit_down(db_path: str, date: str, stocks: list) -> int:
    """插入跌停池数据"""
    try:
        conn = sqlite3.connect(db_path, isolation_level=None)
        cursor = conn.cursor()
        
        count = 0
        for stock in stocks:
            try:
                import sys
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                from xuangutong_fetcher import parse_symbol
                
                code = parse_symbol(stock.get('symbol', ''))
                
                sql = """
                INSERT OR REPLACE INTO xgt_limit_down
                (date, code, name, change_percent, break_times)
                VALUES (?, ?, ?, ?, ?)
                """
                
                params = (
                    date, code,
                    stock.get('stock_chi_name', ''),
                    stock.get('change_percent'),
                    stock.get('break_limit_up_times', 0)
                )
                
                cursor.execute(sql, params)
                count += 1
            except Exception as e:
                logger.warning(f"插入跌停池单条记录失败: {e}")
                continue
        
        conn.close()
        return count
        
    except Exception as e:
        logger.error(f"插入跌停池数据失败: {e}")
        return 0


def get_existing_dates(db_path: str) -> list:
    """
    获取数据库中已有的日期列表
    
    Returns:
        日期字符串列表，按日期排序
    """
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT date FROM xgt_daily_summary ORDER BY date")
        dates = [row[0] for row in cursor.fetchall()]
        conn.close()
        return dates
    except Exception:
        return []


if __name__ == '__main__':
    # 测试建表
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    
    # 尝试从config读取数据库路径
    try:
        from config import DB_PATH
        db_path = DB_PATH
    except ImportError:
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'stock_data_1784791326780_0_09ym.db')
    
    print(f"数据库路径: {db_path}")
    success = init_xgt_tables(db_path)
    print(f"建表结果: {'成功' if success else '失败'}")
    
    # 列出表结构
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    for table in ['xgt_daily_summary', 'xgt_limit_up_detail', 'xgt_break_limit_up', 'xgt_limit_down']:
        cursor.execute(f"PRAGMA table_info({table})")
        cols = cursor.fetchall()
        print(f"\n表 {table} 结构:")
        for col in cols:
            print(f"  {col[1]} ({col[2]})")
    conn.close()
