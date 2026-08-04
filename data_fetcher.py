"""
data_fetcher.py - 数据获取模块
使用akshare获取每日涨停基础数据，使用选股宝API获取概念标签数据
数据清洗和标准化，写入数据库
"""
import logging
import time
import re
from datetime import datetime, timedelta
from collections import Counter

from config import DATA_FILTER_CONFIG

logger = logging.getLogger(__name__)

# 过滤配置
MAIN_BOARD_PREFIXES = DATA_FILTER_CONFIG.get("main_board_prefixes", ("60", "00"))
EXCLUDED_PREFIXES = DATA_FILTER_CONFIG.get("excluded_prefixes", ("30", "68", "8", "400", "420", "430", "830"))
ST_KEYWORDS = DATA_FILTER_CONFIG.get("st_keywords", ("ST", "*ST"))


def is_main_board_stock(code: str, name: str) -> bool:
    """判断是否为主板股票"""
    if not code or not name:
        return False
    code_str = str(code).strip()
    name_str = str(name).strip()

    for kw in ST_KEYWORDS:
        if kw in name_str:
            return False

    if code_str.startswith(MAIN_BOARD_PREFIXES):
        for prefix in EXCLUDED_PREFIXES:
            if code_str.startswith(prefix):
                return False
        return True
    return False


class XGBDataFetcher:
    """选股宝数据获取器"""

    XGB_API_URL = "https://flash-api.xuangubao.cn/api/pool/detail?pool_name=limit_up"
    XGB_HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Accept': 'application/json',
        'Referer': 'https://xuangubao.cn/'
    }
    MAX_RETRIES = 3
    RETRY_DELAY = 2

    def __init__(self, db):
        self.db = db

    def fetch_xgb_data(self, date):
        """获取指定日期的选股宝涨停概念数据"""
        try:
            logger.info(f"[选股宝] 开始获取 {date} 的涨停概念数据...")
            raw_data = self._fetch_xgb_raw_data()
            if not raw_data:
                return 0

            parsed = self.parse_data(raw_data, date)
            if not parsed:
                return 0

            filtered = [r for r in parsed if is_main_board_stock(r.get('code', ''), r.get('name', ''))]
            logger.info(f"[选股宝] 解析{len(parsed)}条，过滤后保留{len(filtered)}条主板股票")

            saved_count = self._save_xgb_data(filtered, date)
            concept_stats = self._calculate_concept_statistics(filtered)
            if concept_stats:
                self._save_concept_stats(concept_stats, date)

            logger.info(f"[选股宝] {date} 数据获取完成: {saved_count}条记录, {len(concept_stats)}个概念")
            return saved_count

        except Exception as e:
            logger.error(f"[选股宝] 获取数据异常: {e}", exc_info=True)
            return 0

    def _fetch_xgb_raw_data(self):
        """调用选股宝API获取原始数据"""
        try:
            import requests
        except ImportError:
            logger.error("[选股宝] requests库未安装")
            return None

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                logger.info(f"[选股宝] 第{attempt}次尝试调用API...")
                response = requests.get(
                    self.XGB_API_URL,
                    headers=self.XGB_HEADERS,
                    timeout=15
                )
                response.raise_for_status()
                result = response.json()

                if result.get("code") != 20000:
                    logger.warning(f"[选股宝] API返回错误码: {result.get('code')}")
                    if attempt < self.MAX_RETRIES:
                        time.sleep(self.RETRY_DELAY)
                        continue
                    return None

                data = result.get("data", [])
                if not data:
                    logger.warning("[选股宝] API返回data为空")
                    return None

                logger.info(f"[选股宝] API返回 {len(data)} 条原始数据")
                return data

            except Exception as e:
                logger.warning(f"[选股宝] 第{attempt}次请求异常: {e}")
                if attempt < self.MAX_RETRIES:
                    time.sleep(self.RETRY_DELAY)

        logger.error(f"[选股宝] {self.MAX_RETRIES}次请求均失败")
        return None

    def parse_data(self, raw_data, date):
        """解析选股宝API返回的原始数据"""
        results = []
        for item in raw_data:
            try:
                symbol = item.get('symbol', '')
                code = re.match(r'^(\d{6})', symbol)
                if not code:
                    continue
                code = code.group(1)

                name = item.get('stock_chi_name', '')
                surge_reason = item.get('surge_reason', {})
                concepts = []
                if surge_reason and isinstance(surge_reason, dict):
                    for plate in surge_reason.get('related_plates', []):
                        plate_name = plate.get('plate_name', '') if isinstance(plate, dict) else ''
                        if plate_name and 'ST' not in plate_name:
                            concepts.append(plate_name.strip())

                reason = ''
                if surge_reason and isinstance(surge_reason, dict):
                    stock_reason = surge_reason.get('stock_reason', '') or ''
                    plate_reason = surge_reason.get('plate_reason', '') or ''
                    if stock_reason and plate_reason:
                        reason = f"{stock_reason} ({plate_reason})"
                    elif stock_reason:
                        reason = stock_reason
                    elif plate_reason:
                        reason = plate_reason

                results.append({
                    'date': date,
                    'code': code,
                    'name': name,
                    'concept': ';'.join(concepts),
                    'reason': reason
                })
            except Exception as e:
                logger.debug(f"[选股宝] 解析单条数据失败: {e}")
                continue

        logger.info(f"[选股宝] 解析完成: {len(results)} 条有效数据")
        return results

    def _save_xgb_data(self, records, date):
        return self.db.save_xgb_detail(records, date)

    def _calculate_concept_statistics(self, records):
        concept_counter = Counter()
        for r in records:
            concept_str = r.get('concept', '')
            if not concept_str:
                continue
            for c in concept_str.split(';'):
                c = c.strip()
                if c and 'ST' not in c:
                    concept_counter[c] += 1
        return [{"concept": concept, "count": count} for concept, count in concept_counter.most_common()]

    def _save_concept_stats(self, stats, date):
        return self.db.save_concept_statistics(stats, date)


class DataFetcher:
    """数据获取器 - 管理akshare基础数据和选股宝概念数据"""

    def __init__(self, db):
        self.db = db
        self.xgb_fetcher = XGBDataFetcher(db)

    def fetch_daily_limit_up(self, date_str):
        """获取指定日期的涨停数据（通过akshare）"""
        try:
            import akshare as ak
            logger.info(f"开始获取 {date_str} 的涨停数据...")

            try:
                df = ak.stock_zt_pool_em(date=date_str.replace("-", ""))
                if df is not None and not df.empty:
                    records = self._parse_akshare_data(df, date_str)
                    self._save_to_db(records, date_str)
                    logger.info(f"成功获取 {date_str} 涨停数据: {len(records)} 条")
                    return len(records)
            except Exception as e:
                logger.warning(f"akshare涨停池接口异常: {e}")

            logger.warning(f"未能获取 {date_str} 的数据（可能是非交易日）")
            return 0

        except ImportError:
            logger.error("akshare未安装")
            return 0
        except Exception as e:
            logger.error(f"获取数据异常: {e}")
            return 0

    def _parse_akshare_data(self, df, date_str):
        """解析akshare返回的数据"""
        records = []
        for _, row in df.iterrows():
            try:
                code = str(row.get("代码", row.get("股票代码", "")))
                name = str(row.get("名称", row.get("股票名称", "")))

                if not is_main_board_stock(code, name):
                    continue

                record = {
                    "date": date_str,
                    "code": code,
                    "name": name,
                    "continuous_boards": int(row.get("连板数", row.get("涨停天数", 1)) or 1),
                    "seal_amount": float(row.get("封板资金", row.get("封单额", 0)) or 0) / 1e8,
                    "seal_style": str(row.get("板风格", row.get("封板类型", "")) or ""),
                    "turnover_rate": float(row.get("换手率", 0) or 0),
                    "latest_price": float(row.get("最新价", row.get("最新价格", 0)) or 0),
                    "change_percent": float(row.get("涨跌幅", 0) or 0),
                }
                if record["code"]:
                    records.append(record)
            except Exception as e:
                logger.debug(f"解析行数据失败: {e}")
                continue
        return records

    def _save_to_db(self, records, date_str):
        for r in records:
            try:
                self.db.execute(
                    """INSERT OR REPLACE INTO akshare_limit_up 
                       (date, code, name, continuous_boards, seal_amount, 
                        seal_style, turnover_rate, latest_price, change_percent)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (r["date"], r["code"], r["name"], r["continuous_boards"],
                     r["seal_amount"], r["seal_style"], r["turnover_rate"],
                     r["latest_price"], r["change_percent"]))
            except Exception as e:
                logger.error(f"保存记录失败: {e}")

    def fetch_concept_data(self, date_str):
        """获取概念板块数据"""
        return self.xgb_fetcher.fetch_xgb_data(date_str)