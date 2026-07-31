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

logger = logging.getLogger(__name__)


class XGBDataFetcher:
    """选股宝数据获取器 - 获取涨停概念标签数据"""

    # 选股宝API地址
    XGB_API_URL = "https://flash-api.xuangubao.cn/api/pool/detail?pool_name=limit_up"

    # 请求头
    XGB_HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Accept': 'application/json',
        'Referer': 'https://xuangubao.cn/'
    }

    # 重试配置
    MAX_RETRIES = 3
    RETRY_DELAY = 2  # 秒

    def __init__(self, db):
        self.db = db

    def fetch_xgb_data(self, date):
        """
        获取指定日期的选股宝涨停概念数据（完整流程）
        返回成功获取的记录数
        """
        try:
            logger.info(f"[选股宝] 开始获取 {date} 的涨停概念数据...")

            # 1. 调用API获取原始数据
            raw_data = self._fetch_xgb_raw_data()
            if not raw_data:
                logger.warning(f"[选股宝] API返回数据为空")
                return 0

            # 2. 解析数据（提取概念、原因）
            parsed = self.parse_data(raw_data, date)
            if not parsed:
                logger.warning(f"[选股宝] 解析后数据为空")
                return 0

            # 3. 过滤主板股票
            filtered = self._filter_main_board(parsed)
            logger.info(f"[选股宝] 解析{len(parsed)}条，过滤后保留{len(filtered)}条主板股票")

            # 4. 保存到xgb_limit_up_detail表
            saved_count = self._save_xgb_data(filtered, date)

            # 5. 计算概念统计并保存到concept_statistics表
            concept_stats = self._calculate_concept_statistics(filtered)
            if concept_stats:
                self._save_concept_stats(concept_stats, date)

            logger.info(f"[选股宝] {date} 数据获取完成: {saved_count}条记录, {len(concept_stats)}个概念")
            return saved_count

        except Exception as e:
            logger.error(f"[选股宝] 获取数据异常: {e}", exc_info=True)
            return 0

    def _fetch_xgb_raw_data(self):
        """
        调用选股宝API获取原始数据
        包含重试机制
        """
        try:
            import requests
        except ImportError:
            logger.error("[选股宝] requests库未安装，请执行: pip install requests")
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

                # 检查返回码
                if result.get("code") != 20000:
                    logger.warning(f"[选股宝] API返回错误码: {result.get('code')}, 信息: {result.get('message', '')}")
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

            except requests.exceptions.Timeout:
                logger.warning(f"[选股宝] 第{attempt}次请求超时")
            except requests.exceptions.ConnectionError:
                logger.warning(f"[选股宝] 第{attempt}次连接失败")
            except Exception as e:
                logger.warning(f"[选股宝] 第{attempt}次请求异常: {e}")

            if attempt < self.MAX_RETRIES:
                logger.info(f"[选股宝] {self.RETRY_DELAY}秒后重试...")
                time.sleep(self.RETRY_DELAY)

        logger.error(f"[选股宝] {self.MAX_RETRIES}次请求均失败")
        return None

    def parse_data(self, raw_data, date):
        """
        解析选股宝API返回的原始数据
        提取股票代码、名称、概念列表、涨停原因
        """
        results = []
        for item in raw_data:
            try:
                # 提取股票代码：格式如 600000.SH → 600000
                symbol = item.get('symbol', '')
                code = self._extract_code(symbol)
                if not code:
                    continue

                # 提取股票名称
                name = item.get('stock_chi_name', '')

                # 提取概念列表（从surge_reason.related_plates中）
                surge_reason = item.get('surge_reason', {})
                concepts = []
                if surge_reason and isinstance(surge_reason, dict):
                    for plate in surge_reason.get('related_plates', []):
                        plate_name = plate.get('plate_name', '') if isinstance(plate, dict) else ''
                        if plate_name and 'ST' not in plate_name:
                            concepts.append(plate_name.strip())

                # 提取涨停原因
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

    def _extract_code(self, symbol):
        """
        从选股宝的symbol字段提取6位股票代码
        格式: 600000.SH → 600000, 000001.SZ → 000001
        """
        if not symbol:
            return ''
        # 匹配6位数字
        match = re.match(r'^(\d{6})', symbol)
        return match.group(1) if match else ''

    def _filter_main_board(self, records):
        """
        过滤只保留主板股票
        主板条件：代码以60或00开头，名称不含ST
        """
        filtered = []
        for r in records:
            code = r.get('code', '')
            name = r.get('name', '')

            # 排除ST股票
            if 'ST' in name or '*ST' in name:
                continue

            # 只保留主板（60开头沪市，00开头深市）
            if code.startswith(('60', '00')):
                filtered.append(r)

        return filtered

    def _save_xgb_data(self, records, date):
        """保存解析后的选股宝数据到数据库"""
        return self.db.save_xgb_detail(records, date)

    def _calculate_concept_statistics(self, records):
        """
        从选股宝记录中计算概念统计
        将每只股票的概念（分号分隔）展开，统计每个概念出现的次数
        返回按数量降序排列的概念统计列表
        """
        concept_counter = Counter()
        for r in records:
            concept_str = r.get('concept', '')
            if not concept_str:
                continue
            for c in concept_str.split(';'):
                c = c.strip()
                if c and 'ST' not in c:
                    concept_counter[c] += 1

        # 转为列表格式
        stats = [{"concept": concept, "count": count}
                 for concept, count in concept_counter.most_common()]
        return stats

    def _save_concept_stats(self, stats, date):
        """保存概念统计到数据库"""
        return self.db.save_concept_statistics(stats, date)


class DataFetcher:
    """数据获取器 - 管理akshare基础数据和选股宝概念数据"""

    def __init__(self, db):
        self.db = db
        self.xgb_fetcher = XGBDataFetcher(db)

    def fetch_daily_limit_up(self, date_str):
        """
        获取指定日期的涨停数据（通过akshare）
        date_str: YYYY-MM-DD格式
        返回: 成功获取的记录数
        """
        try:
            import akshare as ak
            logger.info(f"开始获取 {date_str} 的涨停数据...")
            
            # 尝试使用akshare获取涨停数据
            try:
                df = ak.stock_zt_pool_em(date=date_str.replace("-", ""))
                if df is not None and not df.empty:
                    records = self._parse_akshare_data(df, date_str)
                    self._save_to_db(records, date_str)
                    logger.info(f"成功获取 {date_str} 涨停数据: {len(records)} 条")
                    return len(records)
            except Exception as e:
                logger.warning(f"akshare涨停池接口异常: {e}")
            
            # 备选接口
            try:
                df = ak.stock_zt_pool_dtgc_em(date=date_str.replace("-", ""))
                if df is not None and not df.empty:
                    logger.info(f"涨停板数据已获取: {len(df)} 条")
            except Exception as e:
                logger.debug(f"备选接口也失败: {e}")
            
            logger.warning(f"未能获取 {date_str} 的数据（可能是非交易日）")
            return 0

        except ImportError:
            logger.error("akshare未安装，请执行: pip install akshare")
            return 0
        except Exception as e:
            logger.error(f"获取数据异常: {e}")
            return 0

    def _parse_akshare_data(self, df, date_str):
        """解析akshare返回的数据"""
        records = []
        for _, row in df.iterrows():
            try:
                record = {
                    "date": date_str,
                    "code": str(row.get("代码", row.get("股票代码", ""))),
                    "name": str(row.get("名称", row.get("股票名称", ""))),
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
        """保存到数据库"""
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
        # 提交事务，确保数据持久化
        try:
            self.db.conn.commit()
        except Exception as e:
            logger.error(f"提交数据失败: {e}")

    def fetch_latest_data(self):
        """获取最新交易日的数据"""
        today = datetime.now().strftime("%Y-%m-%d")
        count = self.fetch_daily_limit_up(today)
        
        # 如果今天没数据，尝试昨天
        if count == 0:
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            count = self.fetch_daily_limit_up(yesterday)
        
        return count

    def fetch_concept_data(self, date_str):
        """
        获取概念板块数据（通过选股宝API）
        返回获取的概念记录数
        """
        return self.xgb_fetcher.fetch_xgb_data(date_str)

    def fetch_and_store(self, date_str):
        """
        完整的数据获取流程：
        1. 通过akshare获取涨停基础数据 → akshare_limit_up表
        2. 通过选股宝获取概念标签数据 → xgb_limit_up_detail表 + concept_statistics表
        """
        logger.info(f"=== 开始获取 {date_str} 数据 ===")
        
        # 步骤1：获取涨停基础数据
        limit_count = self.fetch_daily_limit_up(date_str)
        
        # 步骤2：获取概念标签数据（选股宝）
        concept_count = self.fetch_concept_data(date_str)
        
        logger.info(f"数据获取完成: 涨停{limit_count}条, 概念数据{concept_count}条")
        return limit_count
