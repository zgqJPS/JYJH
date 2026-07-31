"""
batch_import.py - 批量导入脚本
独立运行，从选股通API获取历史数据并写入数据库
用法: python batch_import.py --start 2025-01-01 --end 2026-07-29
"""

import sys
import os
import argparse
import json
import logging
import sqlite3
from datetime import datetime, timedelta

# 确保能导入同级目录模块
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from xuangutong_fetcher import (
    fetch_daily_all_data, calculate_daily_metrics, 
    fetch_pool_data, fetch_market_indicators, _is_weekend,
    POOL_NAMES
)
from db_v3_patch import (
    init_xgt_tables, insert_daily_summary, 
    insert_limit_up_details, insert_break_limit_up, 
    insert_limit_down, get_existing_dates
)

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('batch_import')


def get_db_path() -> str:
    """获取数据库路径"""
    # 优先从config.py读取
    try:
        from config import DB_PATH
        if os.path.exists(DB_PATH):
            return DB_PATH
    except ImportError:
        pass
    
    # 默认路径
    default_path = os.path.join(SCRIPT_DIR, 'stock_data_1784791326780_0_09ym.db')
    return default_path


def determine_date_range(start: str = None, end: str = None, db_path: str = None) -> tuple:
    """
    确定日期范围
    如果未指定start，则从数据库已有数据的最后一天+1天开始
    """
    today = datetime.now().strftime('%Y-%m-%d')
    
    if not end:
        end = today
    
    if not start:
        # 从数据库推算
        existing = get_existing_dates(db_path)
        if existing:
            last_date = datetime.strptime(existing[-1], '%Y-%m-%d')
            start = (last_date + timedelta(days=1)).strftime('%Y-%m-%d')
            logger.info(f"检测到数据库已有数据到 {existing[-1]}，从 {start} 开始补录")
        else:
            # 默认从30天前开始
            start = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
            logger.info(f"数据库无历史数据，默认从 {start} 开始")
    
    return start, end


def run_import(start_date: str, end_date: str, db_path: str):
    """
    执行批量导入
    """
    print("=" * 60)
    print(f"选股通历史数据批量导入")
    print(f"日期范围: {start_date} ~ {end_date}")
    print(f"数据库: {db_path}")
    print("=" * 60)
    
    # 1. 初始化数据库表
    print("\n[1/3] 初始化数据表...")
    if not init_xgt_tables(db_path):
        print("ERROR: 数据表初始化失败!")
        return
    
    print("  数据表准备就绪")
    
    # 2. 逐日获取并写入数据
    print(f"\n[2/3] 开始逐日获取数据...")
    
    start = datetime.strptime(start_date, '%Y-%m-%d')
    end = datetime.strptime(end_date, '%Y-%m-%d')
    
    current = start
    stats = {
        'total_days': 0,
        'success_days': 0,
        'failed_days': 0,
        'total_limit_up': 0,
        'total_break': 0,
        'total_limit_down': 0,
        'total_records': 0,
        'daily_details': []
    }
    
    while current <= end:
        date_str = current.strftime('%Y-%m-%d')
        
        # 跳过周末
        if _is_weekend(date_str):
            current += timedelta(days=1)
            continue
        
        stats['total_days'] += 1
        print(f"\n  [{stats['total_days']}] 处理: {date_str}")
        
        try:
            # 获取全部数据
            all_data = fetch_daily_all_data(date_str)
            
            # 计算衍生指标
            metrics = calculate_daily_metrics(date_str, all_data)
            
            # 合并为写入格式
            write_data = {
                'date': date_str,
                'pools': all_data.get('pools', {}),
                'market_indicators': all_data.get('market_indicators', {}),
                'metrics': metrics
            }
            
            # 写入每日汇总
            if insert_daily_summary(db_path, write_data):
                print(f"    ✓ 每日汇总已写入")
            else:
                print(f"    ✗ 每日汇总写入失败")
            
            # 写入涨停池详情
            limit_up_stocks = all_data['pools'].get('limit_up', [])
            lu_count = insert_limit_up_details(db_path, date_str, limit_up_stocks)
            stats['total_limit_up'] += lu_count
            print(f"    ✓ 涨停池: {lu_count} 条")
            
            # 写入炸板池
            break_stocks = all_data['pools'].get('limit_up_broken', [])
            br_count = insert_break_limit_up(db_path, date_str, break_stocks)
            stats['total_break'] += br_count
            print(f"    ✓ 炸板池: {br_count} 条")
            
            # 写入跌停池
            down_stocks = all_data['pools'].get('limit_down', [])
            ld_count = insert_limit_down(db_path, date_str, down_stocks)
            stats['total_limit_down'] += ld_count
            print(f"    ✓ 跌停池: {ld_count} 条")
            
            stats['success_days'] += 1
            stats['total_records'] += lu_count + br_count + ld_count
            
            # 记录每日详情
            indicators = all_data.get('market_indicators', {})
            stats['daily_details'].append({
                'date': date_str,
                'limit_up': lu_count,
                'break': br_count,
                'limit_down': ld_count,
                'explosion_rate': metrics.get('explosion_rate'),
                'market_heat': metrics.get('market_heat'),
                'max_boards': metrics.get('max_continuous_boards'),
                'rise_fall_ratio': metrics.get('rise_fall_ratio')
            })
            
        except Exception as e:
            logger.error(f"处理 {date_str} 时出错: {e}")
            stats['failed_days'] += 1
            stats['daily_details'].append({
                'date': date_str,
                'error': str(e)
            })
        
        current += timedelta(days=1)
        
        # 请求间隔
        if current <= end:
            import time
            time.sleep(1.5)
    
    # 3. 输出统计报告
    print("\n" + "=" * 60)
    print("[3/3] 导入统计报告")
    print("=" * 60)
    print(f"  总交易日数: {stats['total_days']}")
    print(f"  成功: {stats['success_days']}")
    print(f"  失败: {stats['failed_days']}")
    print(f"  涨停记录总数: {stats['total_limit_up']}")
    print(f"  炸板记录总数: {stats['total_break']}")
    print(f"  跌停记录总数: {stats['total_limit_down']}")
    print(f"  个股记录总计: {stats['total_records']}")
    
    print(f"\n  ===== 每日明细 =====")
    print(f"  {'日期':<12} {'涨停':>6} {'炸板':>6} {'跌停':>6} {'炸板率':>8} {'涨跌比':>8} {'最高连板':>8} {'热度':>6}")
    print(f"  {'-'*12} {'-'*6} {'-'*6} {'-'*6} {'-'*8} {'-'*8} {'-'*8} {'-'*6}")
    
    for detail in stats['daily_details']:
        if 'error' in detail:
            print(f"  {detail['date']:<12} ERROR: {detail['error']}")
            continue
        
        er = f"{detail['explosion_rate']:.1%}" if detail.get('explosion_rate') is not None else '-'
        rfr = f"{detail['rise_fall_ratio']:.2f}" if detail.get('rise_fall_ratio') is not None else '-'
        mh = f"{detail['market_heat']:.0f}" if detail.get('market_heat') is not None else '-'
        
        print(f"  {detail['date']:<12} {detail['limit_up']:>6} {detail['break']:>6} "
              f"{detail['limit_down']:>6} {er:>8} {rfr:>8} {detail.get('max_boards', '-'):>8} {mh:>6}")
    
    # 保存统计报告到JSON
    report_path = os.path.join(SCRIPT_DIR, 'xgt_import_report.json')
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump({
            'import_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'date_range': {'start': start_date, 'end': end_date},
            'stats': {k: v for k, v in stats.items() if k != 'daily_details'},
            'daily_details': stats['daily_details']
        }, f, ensure_ascii=False, indent=2)
    print(f"\n  统计报告已保存: {report_path}")
    
    print("\n" + "=" * 60)
    print("导入完成!")
    print("=" * 60)
    
    return stats


def main():
    parser = argparse.ArgumentParser(description='选股通历史数据批量导入')
    parser.add_argument('--start', type=str, help='开始日期 YYYY-MM-DD')
    parser.add_argument('--end', type=str, help='结束日期 YYYY-MM-DD')
    parser.add_argument('--db', type=str, help='数据库路径（可选）')
    
    args = parser.parse_args()
    
    db_path = args.db or get_db_path()
    start_date, end_date = determine_date_range(args.start, args.end, db_path)
    
    run_import(start_date, end_date, db_path)


if __name__ == '__main__':
    main()
