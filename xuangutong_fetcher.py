"""
xuangutong_fetcher.py - 选股通/选股宝 API 数据爬取模块
从选股宝API获取每日市场数据，包括各股票池和市场指标
"""

import requests
import time
import logging
import json
import re
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any

# 日志配置
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# API基础URL
BASE_URL = "https://flash-api.xuangubao.cn/api"

# 请求头
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Accept': 'application/json',
    'Referer': 'https://xuangutong.com.cn/'
}

# 股票池类型
POOL_NAMES = {
    'limit_up': '涨停池',
    'limit_up_broken': '炸板池',
    'yesterday_limit_up': '昨日涨停今日表现',
    'limit_down': '跌停池',
    'new_stock': '新股池',
}

# 市场指标字段
MARKET_INDICATOR_FIELDS = 'limit_up_count,limit_down_count,rise_count,fall_count'

# 重试配置
MAX_RETRIES = 3
RETRY_DELAY = 2  # 秒


def parse_symbol(symbol: str) -> str:
    """
    从 "601858.SS" 格式提取纯数字代码 "601858"
    """
    if not symbol:
        return ""
    return symbol.split('.')[0]


def _request_with_retry(url: str, params: dict, retries: int = MAX_RETRIES) -> Optional[Dict]:
    """
    带重试机制的HTTP请求
    """
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data.get('code') == 20000:
                return data
            else:
                logger.warning(f"API返回非20000状态码: code={data.get('code')}, url={url}, params={params}")
                return data  # 仍然返回，让调用方判断
        except requests.exceptions.RequestException as e:
            logger.warning(f"请求失败(第{attempt}/{retries}次): {url} - {e}")
            if attempt < retries:
                time.sleep(RETRY_DELAY)
            else:
                logger.error(f"请求最终失败: {url}")
                return None
    return None


def fetch_pool_data(pool_name: str, date: str) -> List[Dict]:
    """
    获取指定日期的指定池数据
    
    Args:
        pool_name: 池名称，如 'limit_up', 'break_limit_up' 等
        date: 日期，格式 'YYYY-MM-DD'
    
    Returns:
        股票列表，每条记录包含 symbol, stock_chi_name, price, change_percent 等字段
    """
    url = f"{BASE_URL}/pool/detail"
    params = {
        'pool_name': pool_name,
        'date': date
    }
    
    logger.info(f"获取池数据: pool={pool_name}({POOL_NAMES.get(pool_name, '未知')}), date={date}")
    
    result = _request_with_retry(url, params)
    if result and result.get('code') == 20000:
        data = result.get('data', [])
        logger.info(f"  -> 获取到 {len(data)} 条记录")
        return data
    else:
        logger.warning(f"  -> 未获取到池 {pool_name} 在 {date} 的数据")
        return []


def fetch_market_indicators(date: str) -> Dict[str, Any]:
    """
    获取市场指标（涨停数、跌停数、涨跌家数）
    从分钟级时序数据中取最后一个非null值作为收盘数据
    
    Args:
        date: 日期，格式 'YYYY-MM-DD'
    
    Returns:
        字典，包含 limit_up_count, limit_down_count, rise_count, fall_count
    """
    url = f"{BASE_URL}/market_indicator/line"
    params = {
        'fields': MARKET_INDICATOR_FIELDS,
        'date': date
    }
    
    logger.info(f"获取市场指标: date={date}")
    
    result = _request_with_retry(url, params)
    if not result or result.get('code') != 20000:
        logger.warning(f"  -> 未获取到市场指标数据")
        return {}
    
    data_list = result.get('data', [])
    if not data_list:
        logger.warning(f"  -> 市场指标数据为空")
        return {}
    
    # 取最后一个非null值作为当日收盘数据
    indicators = {
        'limit_up_count': None,
        'limit_down_count': None,
        'rise_count': None,
        'fall_count': None
    }
    
    # 倒序遍历，找到每个字段的最后一个非null值
    for field in indicators:
        for item in reversed(data_list):
            val = item.get(field)
            if val is not None:
                indicators[field] = val
                break
    
    logger.info(f"  -> 市场指标: 涨停={indicators['limit_up_count']}, "
                f"跌停={indicators['limit_down_count']}, "
                f"上涨={indicators['rise_count']}, 下跌={indicators['fall_count']}")
    return indicators


def fetch_daily_all_data(date: str) -> Dict[str, Any]:
    """
    获取某日全部数据（所有池 + 市场指标）
    
    Args:
        date: 日期，格式 'YYYY-MM-DD'
    
    Returns:
        完整数据字典
    """
    logger.info(f"===== 开始获取 {date} 全部数据 =====")
    
    all_data = {
        'date': date,
        'pools': {},
        'market_indicators': {},
        'fetch_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    
    # 获取各池数据
    for pool_name in POOL_NAMES:
        pool_data = fetch_pool_data(pool_name, date)
        all_data['pools'][pool_name] = pool_data
        time.sleep(0.5)  # 请求间隔0.5秒
    
    # 获取市场指标
    indicators = fetch_market_indicators(date)
    all_data['market_indicators'] = indicators
    
    logger.info(f"===== {date} 数据获取完成 =====")
    return all_data


def calculate_daily_metrics(date: str, all_data: Optional[Dict] = None) -> Dict[str, Any]:
    """
    计算衍生指标
    
    Args:
        date: 日期
        all_data: 已获取的完整数据，若为None则自动获取
    
    Returns:
        衍生指标字典
    """
    if all_data is None:
        all_data = fetch_daily_all_data(date)
    
    metrics = {
        'date': date,
        'explosion_rate': None,       # 炸板率
        'rise_fall_ratio': None,      # 涨跌比
        'board_distribution': {},     # 连板分布
        'yesterday_limit_up_avg_change': None,  # 昨日涨停今日表现
        'market_heat': None,          # 市场真实热度(0-100)
        'max_continuous_boards': 0,   # 最高连板
    }
    
    pools = all_data.get('pools', {})
    indicators = all_data.get('market_indicators', {})
    
    # 1. 炸板率 = 炸板数 / (涨停数 + 炸板数)
    limit_up_list = pools.get('limit_up', [])
    break_list = pools.get('limit_up_broken', [])
    
    limit_up_count = len(limit_up_list)
    break_count = len(break_list)
    
    # 也使用市场指标中的涨停数（取较大值）
    mi_limit_up = indicators.get('limit_up_count')
    if mi_limit_up is not None:
        limit_up_count = max(limit_up_count, mi_limit_up)
    
    if limit_up_count + break_count > 0:
        metrics['explosion_rate'] = round(break_count / (limit_up_count + break_count), 4)
    
    # 2. 涨跌比
    rise_count = indicators.get('rise_count')
    fall_count = indicators.get('fall_count')
    if rise_count is not None and fall_count is not None and fall_count > 0:
        metrics['rise_fall_ratio'] = round(rise_count / fall_count, 4)
    
    # 3. 连板分布 & 最高连板
    board_dist = {}
    for stock in limit_up_list:
        days = stock.get('limit_up_days', 1) or 1
        board_dist[days] = board_dist.get(days, 0) + 1
    
    metrics['board_distribution'] = board_dist
    if board_dist:
        metrics['max_continuous_boards'] = max(board_dist.keys())
    
    # 4. 昨日涨停今日表现（均涨幅）
    yesterday_list = pools.get('yesterday_limit_up', [])
    if yesterday_list:
        changes = []
        for stock in yesterday_list:
            cp = stock.get('change_percent')
            if cp is not None:
                changes.append(cp)
        if changes:
            metrics['yesterday_limit_up_avg_change'] = round(sum(changes) / len(changes), 4)
    
    # 5. 市场真实热度（综合评分 0-100）
    heat_score = 50.0  # 基准分
    
    # 涨停数量贡献 (涨停越多越热)
    if limit_up_count > 0:
        heat_score += min(limit_up_count * 0.5, 20)  # 最多+20
    
    # 炸板率影响（炸板率越高越冷）
    if metrics['explosion_rate'] is not None:
        heat_score -= metrics['explosion_rate'] * 30  # 炸板率0.5时减15分
    
    # 涨跌比影响
    if metrics['rise_fall_ratio'] is not None:
        if metrics['rise_fall_ratio'] > 1:
            heat_score += min((metrics['rise_fall_ratio'] - 1) * 10, 15)
        else:
            heat_score -= min((1 - metrics['rise_fall_ratio']) * 10, 15)
    
    # 连板高度影响
    if metrics['max_continuous_boards'] > 0:
        heat_score += min(metrics['max_continuous_boards'] * 2, 10)
    
    # 昨日涨停今日表现
    if metrics['yesterday_limit_up_avg_change'] is not None:
        avg_cp = metrics['yesterday_limit_up_avg_change']
        # 涨幅为正说明赚钱效应好
        heat_score += min(max(avg_cp * 5, -10), 10)
    
    # 限制在0-100范围
    metrics['market_heat'] = round(max(0, min(100, heat_score)), 2)
    
    logger.info(f"衍生指标: 炸板率={metrics['explosion_rate']}, "
                f"涨跌比={metrics['rise_fall_ratio']}, "
                f"最高连板={metrics['max_continuous_boards']}, "
                f"市场热度={metrics['market_heat']}")
    
    return metrics


def _is_weekend(date_str: str) -> bool:
    """判断是否为周末"""
    dt = datetime.strptime(date_str, '%Y-%m-%d')
    return dt.weekday() >= 5  # 5=Saturday, 6=Sunday


def batch_fetch_historical(start_date: str, end_date: str, 
                           callback=None) -> List[Dict]:
    """
    批量获取历史数据
    
    Args:
        start_date: 开始日期 'YYYY-MM-DD'
        end_date: 结束日期 'YYYY-MM-DD'
        callback: 每日数据获取完成后的回调函数 callback(date, all_data, metrics)
    
    Returns:
        所有日期的数据列表
    """
    logger.info(f"===== 批量获取历史数据: {start_date} ~ {end_date} =====")
    
    results = []
    start = datetime.strptime(start_date, '%Y-%m-%d')
    end = datetime.strptime(end_date, '%Y-%m-%d')
    
    current = start
    trading_days = 0
    total_days = (end - start).days + 1
    
    while current <= end:
        date_str = current.strftime('%Y-%m-%d')
        
        # 跳过周末
        if _is_weekend(date_str):
            current += timedelta(days=1)
            continue
        
        trading_days += 1
        logger.info(f"[{trading_days}/{total_days}] 处理交易日: {date_str}")
        
        # 获取全部数据
        all_data = fetch_daily_all_data(date_str)
        
        # 计算衍生指标
        metrics = calculate_daily_metrics(date_str, all_data)
        
        # 合并结果
        day_result = {
            'date': date_str,
            'pools': all_data.get('pools', {}),
            'market_indicators': all_data.get('market_indicators', {}),
            'metrics': metrics,
            'fetch_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        results.append(day_result)
        
        # 回调
        if callback:
            callback(date_str, all_data, metrics)
        
        # 请求间隔
        current += timedelta(days=1)
        if current <= end:
            sleep_time = 1.5  # 间隔1.5秒
            logger.info(f"  等待 {sleep_time}s 后继续...")
            time.sleep(sleep_time)
    
    logger.info(f"===== 批量获取完成，共获取 {len(results)} 个交易日数据 =====")
    return results


# 辅助函数：解析涨停原因和概念
def parse_surge_reason(stock: Dict) -> tuple:
    """
    解析涨停原因和关联概念
    
    Returns:
        (reason, concepts_str) - 原因文本和分号分隔的概念名
    """
    surge_reason = stock.get('surge_reason', {})
    if not surge_reason:
        return "", ""
    
    reason = surge_reason.get('stock_reason', '') or ''
    
    plates = surge_reason.get('related_plates', []) or []
    concepts = []
    for plate in plates:
        plate_name = plate.get('plate_name', '')
        if plate_name:
            concepts.append(plate_name)
    
    return reason, ';'.join(concepts)


def format_timestamp(ts) -> Optional[str]:
    """
    将时间戳转为 HH:MM:SS 格式时间字符串
    """
    if ts is None or ts == 0:
        return None
    try:
        # 选股宝的时间戳可能是秒级
        dt = datetime.fromtimestamp(ts)
        return dt.strftime('%H:%M:%S')
    except (ValueError, OSError, TypeError):
        return None


if __name__ == '__main__':
    # 测试单个日期的数据获取
    test_date = '2026-07-28'
    print(f"测试获取 {test_date} 的数据...")
    
    all_data = fetch_daily_all_data(test_date)
    metrics = calculate_daily_metrics(test_date, all_data)
    
    print("\n===== 数据概览 =====")
    for pool_name, pool_data in all_data['pools'].items():
        print(f"  {POOL_NAMES.get(pool_name, pool_name)}: {len(pool_data)} 条")
    
    print(f"\n===== 市场指标 =====")
    for k, v in all_data['market_indicators'].items():
        print(f"  {k}: {v}")
    
    print(f"\n===== 衍生指标 =====")
    for k, v in metrics.items():
        if k != 'date' and k != 'board_distribution':
            print(f"  {k}: {v}")
    print(f"  board_distribution: {json.dumps(metrics.get('board_distribution', {}), ensure_ascii=False)}")
