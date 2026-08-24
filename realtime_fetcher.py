"""
realtime_fetcher.py - 从选股通盯盘页直接获取当天实时数据
用于获取当天最新数据，比API更及时
"""

import requests
import json
import time
import logging
from datetime import datetime
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)

# 盯盘页数据API端点
DINGPAN_URLS = {
    'limit_up': 'https://flash-api.xuangubao.cn/api/pool/detail?pool_name=limit_up',
    'limit_up_broken': 'https://flash-api.xuangubao.cn/api/pool/detail?pool_name=limit_up_broken',
    'limit_down': 'https://flash-api.xuangubao.cn/api/pool/detail?pool_name=limit_down',
    'yesterday_limit_up': 'https://flash-api.xuangubao.cn/api/pool/detail?pool_name=yesterday_limit_up',
    'market_indicator': 'https://flash-api.xuangubao.cn/api/market_indicator/line?fields=limit_up_count,limit_down_count,rise_count,fall_count',
}

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Referer': 'https://xuangutong.com.cn/dingpan',
    'Origin': 'https://xuangutong.com.cn',
    'Connection': 'keep-alive',
}


def fetch_realtime_today() -> Dict[str, Any]:
    """获取当天实时数据"""
    logger.info("=" * 60)
    logger.info("开始获取当天实时数据（盯盘页模式）")
    logger.info("=" * 60)
    
    result = {
        'pools': {},
        'market_indicators': {},
        'date': datetime.now().strftime('%Y-%m-%d'),
        'fetch_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'source': 'realtime_dingpan'
    }
    
    # 1. 获取各股票池数据
    for pool_name, url in DINGPAN_URLS.items():
        if pool_name == 'market_indicator':
            continue
        try:
            data = _fetch_pool_realtime(url, pool_name)
            result['pools'][pool_name] = data
            logger.info(f"[盯盘] {pool_name}: {len(data)} 只")
            time.sleep(0.3)
        except Exception as e:
            logger.error(f"[盯盘] 获取{pool_name}失败: {e}")
            result['pools'][pool_name] = []
    
    # 2. 获取市场指标
    try:
        indicators = _fetch_market_indicators_realtime()
        result['market_indicators'] = indicators
        logger.info(f"[盯盘] 市场指标: {indicators}")
    except Exception as e:
        logger.error(f"[盯盘] 获取市场指标失败: {e}")
    
    # 3. 推断真实日期
    limit_up_data = result['pools'].get('limit_up', [])
    if limit_up_data:
        first_stock = limit_up_data[0]
        data_date = first_stock.get('trade_date', '')
        if data_date:
            result['date'] = data_date
            logger.info(f"[盯盘] 数据实际日期: {data_date}")
    
    logger.info("=" * 60)
    logger.info(f"实时数据获取完成，日期: {result['date']}")
    logger.info(f"涨停: {len(result['pools'].get('limit_up', []))}只")
    logger.info(f"炸板: {len(result['pools'].get('limit_up_broken', []))}只")
    logger.info("=" * 60)
    
    return result


def _fetch_pool_realtime(url: str, pool_name: str) -> List[Dict]:
    """获取单个股票池的实时数据"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get('code') != 20000:
            logger.warning(f"[盯盘] {pool_name} API返回错误: {data.get('message', '')}")
            return []
        raw_items = data.get('data', [])
        if not isinstance(raw_items, list):
            logger.warning(f"[盯盘] {pool_name} data不是list类型: {type(raw_items)}")
            return []
        stocks = []
        for item in raw_items:
            stock = _parse_stock_item(item, pool_name)
            if stock:
                stocks.append(stock)
        return stocks
    except requests.exceptions.RequestException as e:
        logger.error(f"[盯盘] 网络请求失败({pool_name}): {e}")
        return []
    except json.JSONDecodeError as e:
        logger.error(f"[盯盘] JSON解析失败({pool_name}): {e}")
        return []


def _fetch_market_indicators_realtime() -> Dict[str, Any]:
    """获取市场指标的实时数据"""
    url = DINGPAN_URLS['market_indicator']
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get('code') != 20000:
            return {}
        result_data = data.get('data', {})
        indicators = {}
        if isinstance(result_data, list):
            for item in result_data:
                if isinstance(item, dict):
                    for key in ['limit_up_count', 'limit_down_count', 'rise_count', 'fall_count']:
                        if key in item:
                            indicators[key] = item[key]
        elif isinstance(result_data, dict):
            for field, values in result_data.items():
                if isinstance(values, list) and len(values) > 0:
                    latest = values[-1]
                    indicators[field] = latest.get('value')
                    if 'trade_date' in latest:
                        indicators['data_date'] = latest['trade_date']
        return indicators
    except Exception as e:
        logger.error(f"[盯盘] 获取市场指标失败: {e}")
        return {}


def _parse_stock_item(item: Dict, pool_name: str) -> Optional[Dict]:
    """解析单只股票数据，并对异常连板数进行修正"""
    try:
        symbol = item.get('symbol', '')
        code = symbol.split('.')[0] if '.' in symbol else symbol
        
        concept = ''
        surge_reason = item.get('surge_reason', {})
        if surge_reason and isinstance(surge_reason, dict):
            plates = surge_reason.get('related_plates', [])
            if plates and len(plates) > 0:
                concept = plates[0].get('plate_name', '')
        
        first_limit_up_ts = item.get('first_limit_up', 0)
        first_limit_up_time = ''
        if first_limit_up_ts:
            try:
                dt = datetime.fromtimestamp(first_limit_up_ts)
                first_limit_up_time = dt.strftime('%H:%M:%S')
            except:
                pass
        
        # 获取原始数据并转为整数
        limit_up_days = int(item.get('limit_up_days', 1) or 1)
        break_times = int(item.get('break_limit_up_times', 0) or 0)
        
        # ★★★ 核心修正：如果连板数与开板次数相等且大于3，则强制设为1（API错误）★★★
        if limit_up_days == break_times and limit_up_days > 3:
            logger.warning(f"[解析] 股票{code} 连板数{limit_up_days}与开板次数{break_times}相等，疑似API数据错误，强制设为1板")
            limit_up_days = 1
        
        stock = {
            'code': code,
            'name': item.get('stock_chi_name', ''),
            'change_percent': item.get('change_percent', 0) or 0,
            'latest_price': item.get('price', 0) or 0,
            'turnover_rate': item.get('turnover_ratio', 0) or 0,
            'seal_amount': item.get('buy_lock_volume_ratio', 0) or 0,
            'seal_ratio': item.get('buy_lock_volume_ratio', 0) or 0,
            'limit_up_days': limit_up_days,          # 修正后的连板数
            'first_limit_up_time': first_limit_up_time,
            'open_times': break_times,               # 开板次数
            'volume_ratio': item.get('volume_bias_ratio', 1.0) or 1.0,
            'flow_capital': item.get('non_restricted_capital', 0) or 0,
            'total_capital': item.get('total_capital', 0) or 0,
            'concept': concept,
            'trade_date': '',
            '_pool': pool_name,
        }
        
        if not stock['code'] or stock['code'] in ('', 'None'):
            return None
        
        # 确保数值类型
        for key in ['change_percent', 'latest_price', 'turnover_rate', 'seal_amount', 'seal_ratio', 'flow_capital', 'volume_ratio']:
            if stock[key] is None:
                stock[key] = 0
            else:
                try:
                    stock[key] = float(stock[key])
                except (ValueError, TypeError):
                    stock[key] = 0
        
        # 流通市值转为亿元
        if stock['flow_capital'] > 1e8:
            stock['flow_capital'] = stock['flow_capital'] / 1e8
        
        return stock
    except Exception as e:
        logger.debug(f"[盯盘] 解析股票数据失败: {e}")
        return None


def save_realtime_to_db(db_path: str, data: Dict[str, Any]) -> int:
    """
    将实时数据保存到数据库，并在入库后进行异常修正和重新统计。
    """
    import sqlite3
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    saved_count = 0
    date = data['date']
    
    try:
        # 1. 保存涨停详情到 xgt_limit_up_detail（原始数据，但已在解析时修正）
        limit_up_stocks = data['pools'].get('limit_up', [])
        for stock in limit_up_stocks:
            try:
                conn.execute("""
                    INSERT OR REPLACE INTO xgt_limit_up_detail 
                    (date, code, name, price, change_percent, turnover_rate,
                     seal_ratio, limit_up_days, first_limit_up_time,
                     break_times, volume_bias, flow_capital, total_capital, concept, reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    date, stock['code'], stock['name'],
                    stock.get('latest_price', 0),
                    stock.get('change_percent', 0),
                    stock.get('turnover_rate', 0),
                    stock.get('seal_ratio', 0),
                    stock.get('limit_up_days', 1),
                    stock.get('first_limit_up_time', ''),
                    stock.get('open_times', 0),
                    stock.get('volume_ratio', 1.0),
                    stock.get('flow_capital', 0),
                    stock.get('total_capital', 0),
                    stock.get('concept', ''),
                    ''
                ))
                saved_count += 1
            except Exception as e:
                logger.warning(f"保存涨停详情失败({stock.get('code', '')}): {e}")
        
        # ★★★ 二次修正：批量处理入库后仍存在的异常数据（以防解析阶段遗漏）★★★
        affected = conn.execute("""
            UPDATE xgt_limit_up_detail 
            SET limit_up_days = 1 
            WHERE date = ? 
              AND limit_up_days > 3 
              AND limit_up_days = break_times
        """, (date,)).rowcount
        if affected > 0:
            logger.warning(f"[修正] 批量修正了 {affected} 只股票的异常连板数（limit_up_days == break_times）")
        
        # 2. 保存炸板池
        break_stocks = data['pools'].get('limit_up_broken', [])
        for stock in break_stocks:
            try:
                conn.execute("""
                    INSERT OR REPLACE INTO xgt_break_limit_up
                    (date, code, name, change_percent, limit_up_days, break_times, concept)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    date, stock['code'], stock['name'],
                    stock.get('change_percent', 0),
                    stock.get('limit_up_days', 1),
                    stock.get('open_times', 0),
                    stock.get('concept', '')
                ))
                saved_count += 1
            except Exception as e:
                logger.warning(f"保存炸板数据失败({stock.get('code', '')}): {e}")
        
        # 3. 保存跌停池
        limit_down_stocks = data['pools'].get('limit_down', [])
        for stock in limit_down_stocks:
            try:
                conn.execute("""
                    INSERT OR REPLACE INTO xgt_limit_down
                    (date, code, name, change_percent, break_times)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    date, stock['code'], stock['name'],
                    stock.get('change_percent', 0),
                    stock.get('open_times', 0)
                ))
                saved_count += 1
            except Exception as e:
                logger.warning(f"保存跌停数据失败({stock.get('code', '')}): {e}")
        
        # 4. 概念统计
        concept_count = {}
        for stock in limit_up_stocks:
            concept = stock.get('concept', '')
            if concept and concept not in ('', 'None'):
                concept_count[concept] = concept_count.get(concept, 0) + 1
        for stock in break_stocks:
            concept = stock.get('concept', '')
            if concept and concept not in ('', 'None'):
                key = f"{concept}(炸板)"
                concept_count[key] = concept_count.get(key, 0) + 1
        
        if concept_count:
            for concept, count in sorted(concept_count.items(), key=lambda x: x[1], reverse=True):
                try:
                    conn.execute("""
                        INSERT OR REPLACE INTO concept_statistics
                        (date, concept, count)
                        VALUES (?, ?, ?)
                    """, (date, concept, count))
                except Exception as e:
                    logger.debug(f"保存概念统计失败({concept}): {e}")
            logger.info(f"[概念统计] {date}: 共{len(concept_count)}个概念, TOP3: {sorted(concept_count.items(), key=lambda x: x[1], reverse=True)[:3]}")
        
        # 5. 重新统计板分布（因为可能已修正）
        dist_rows = conn.execute("""
            SELECT limit_up_days, COUNT(*) as cnt FROM xgt_limit_up_detail 
            WHERE date = ? GROUP BY limit_up_days
        """, (date,)).fetchall()
        board_dist = {r[0]: r[1] for r in dist_rows}
        max_boards = max(board_dist.keys()) if board_dist else 0
        
        # 6. 每日汇总
        indicators = data.get('market_indicators', {})
        limit_up_count = sum(board_dist.values())
        break_count = len(break_stocks)
        limit_down_count = len(limit_down_stocks)
        explosion_rate = break_count / (limit_up_count + break_count) if (limit_up_count + break_count) > 0 else 0
        rise_count = indicators.get('rise_count', 0) or 0
        fall_count = indicators.get('fall_count', 0) or 0
        rise_fall_ratio = rise_count / fall_count if fall_count > 0 else 1.0
        
        try:
            conn.execute("""
                INSERT OR REPLACE INTO xgt_daily_summary
                (date, limit_up_count, limit_down_count, break_limit_up_count,
                 rise_count, fall_count, explosion_rate, rise_fall_ratio,
                 market_heat, max_continuous_boards, board_distribution)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                date,
                limit_up_count,
                limit_down_count,
                break_count,
                rise_count,
                fall_count,
                explosion_rate,
                rise_fall_ratio,
                0,
                max_boards,
                json.dumps(board_dist)
            ))
        except Exception as e:
            logger.error(f"保存每日汇总失败: {e}")
        
        # 7. 砸盘系数计算（使用修正后的 max_boards）
        try:
            # 获取前一交易日
            prev_row = conn.execute("""
                SELECT date FROM xgt_limit_up_detail 
                WHERE date < ? GROUP BY date ORDER BY date DESC LIMIT 1
            """, (date,)).fetchone()
            
            smash_coeff = None
            
            if prev_row:
                prev_date = prev_row['date'] if isinstance(prev_row, sqlite3.Row) else prev_row[0]
                # 获取今日和昨日的连板分布
                today_boards = {}
                for r in conn.execute("""
                    SELECT limit_up_days, COUNT(*) as cnt FROM xgt_limit_up_detail 
                    WHERE date = ? GROUP BY limit_up_days
                """, (date,)).fetchall():
                    today_boards[r['limit_up_days']] = r['cnt']
                
                prev_boards = {}
                for r in conn.execute("""
                    SELECT limit_up_days, COUNT(*) as cnt FROM xgt_limit_up_detail 
                    WHERE date = ? GROUP BY limit_up_days
                """, (prev_date,)).fetchall():
                    prev_boards[r['limit_up_days']] = r['cnt']
                
                ratios = []
                max_board = max(today_boards.keys()) if today_boards else 0
                for n in range(2, max_board + 1):
                    today_n = today_boards.get(n, 0)
                    prev_n1 = prev_boards.get(n - 1, 0)
                    if prev_n1 > 0 and today_n > 0:
                        ratios.append(today_n / prev_n1)
                
                if ratios:
                    smash_coeff = round(sum(ratios) / len(ratios) * 10, 2)
                    logger.info(f"[砸盘系数] 计算得到 {date}: {smash_coeff} (基于{len(ratios)}个晋升比率, 前日{prev_date})")
                else:
                    first_board = today_boards.get(1, 1)
                    high_board = sum(today_boards.get(n, 0) for n in range(2, max_board + 1))
                    if first_board > 0:
                        smash_coeff = round(high_board / first_board * 10, 2)
                        logger.info(f"[砸盘系数] 使用简化估算 {date}: {smash_coeff} (高板/首板={high_board}/{first_board})")
                    else:
                        logger.info(f"[砸盘系数] {date}: 无有效晋升比率，简化估算失败")
            else:
                logger.info(f"[砸盘系数] {date}: 无前日数据，尝试从历史估算")
                recent_rows = conn.execute("""
                    SELECT smash_coefficient FROM smash_coefficients
                    WHERE trade_date < ? AND smash_coefficient IS NOT NULL
                    ORDER BY trade_date DESC LIMIT 3
                """, (date,)).fetchall()
                if recent_rows:
                    values = [r['smash_coefficient'] for r in recent_rows if r['smash_coefficient'] is not None]
                    if values:
                        avg_smash = sum(values) / len(values)
                        smash_coeff = round(avg_smash, 2)
                        logger.info(f"[砸盘系数] 使用最近{len(values)}天均值估算 {date}: {smash_coeff}")
                    else:
                        smash_coeff = None
                else:
                    logger.info(f"[砸盘系数] {date}: 无历史数据，无法估算")
            
            # 写入砸盘系数
            conn.execute("""
                INSERT OR REPLACE INTO smash_coefficients 
                (trade_date, smash_coefficient, limit_up_count, max_continuous_days)
                VALUES (?, ?, ?, ?)
            """, (date, smash_coeff, limit_up_count, max_boards))
            logger.info(f"[砸盘系数] {date}: 存储值为 {smash_coeff}, 最高板 {max_boards}")
        except Exception as e:
            logger.warning(f"[砸盘系数] 计算失败(不影响其他数据): {e}")
        
        conn.commit()
        logger.info(f"[盯盘] 数据已保存到数据库，共{saved_count}条记录")
        
    except Exception as e:
        logger.error(f"[盯盘] 保存数据异常: {e}")
        conn.rollback()
    finally:
        conn.close()
    
    return saved_count


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    data = fetch_realtime_today()
    print(f"\n获取完成:")
    print(f"  日期: {data['date']}")
    print(f"  涨停: {len(data['pools'].get('limit_up', []))}只")
    print(f"  炸板: {len(data['pools'].get('limit_up_broken', []))}只")
    print(f"  跌停: {len(data['pools'].get('limit_down', []))}只")