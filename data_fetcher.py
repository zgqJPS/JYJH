"""
data_fetcher.py - 数据获取模块
使用akshare获取每日涨停基础数据，使用选股宝API获取概念标签数据
数据清洗和标准化，写入数据库（统一使用xgt_limit_up_detail表）
"""
import logging
import time
import re
from datetime import datetime, timedelta
from collections import Counter

logger = logging.getLogger(__name__)


class XGBDataFetcher:
    """选股宝数据获取器 - 获取涨停概念标签数据（已废弃，改用realtime_fetcher）"""
    # 保留但不再使用，以免重复
    pass


class DataFetcher:
    """数据获取器 - 管理akshare基础数据（写入xgt_limit_up_detail）"""

    def __init__(self, db):
        self.db = db

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
                    records = self._parse_akshare_data(df, date_str)
                    self._save_to_db(records, date_str)
                    logger.info(f"成功获取 {date_str} 涨停数据（备选接口）: {len(records)} 条")
                    return len(records)
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
        """解析akshare返回的数据，映射到xgt_limit_up_detail字段"""
        records = []
        for _, row in df.iterrows():
            try:
                # 提取字段
                code = str(row.get("代码", row.get("股票代码", "")))
                name = str(row.get("名称", row.get("股票名称", "")))
                limit_up_days = int(row.get("连板数", row.get("涨停天数", 1)) or 1)
                seal_amount_billion = float(row.get("封板资金", row.get("封单额", 0)) or 0) / 1e8  # 转为亿元
                # 近似seal_ratio：封单额(亿元) / 100 得到 0~1 的比例（因为流通市值通常几十到几百亿）
                seal_ratio = seal_amount_billion / 100.0
                if seal_ratio > 1.0:
                    seal_ratio = 1.0
                turnover_rate = float(row.get("换手率", 0) or 0)
                price = float(row.get("最新价", row.get("最新价格", 0)) or 0)
                change_percent = float(row.get("涨跌幅", 0) or 0)
                # 其他字段
                first_limit_up_time = row.get("首次封板时间", "")
                break_times = int(row.get("开板次数", 0) or 0)

                record = {
                    "date": date_str,
                    "code": code,
                    "name": name,
                    "price": price,
                    "change_percent": change_percent,
                    "limit_up_days": limit_up_days,
                    "first_limit_up_time": first_limit_up_time,
                    "break_times": break_times,
                    "seal_ratio": round(seal_ratio, 4),
                    "turnover_rate": turnover_rate,
                    "volume_bias": 1.0,  # akshare不提供，默认1
                    "flow_capital": None,
                    "total_capital": None,
                    "concept": "",
                    "reason": "",
                }
                if record["code"]:
                    records.append(record)
            except Exception as e:
                logger.debug(f"解析行数据失败: {e}")
                continue
        return records

    def _save_to_db(self, records, date_str):
        """保存到xgt_limit_up_detail表"""
        for r in records:
            try:
                self.db.execute(
                    """INSERT OR REPLACE INTO xgt_limit_up_detail 
                       (date, code, name, price, change_percent, limit_up_days,
                        first_limit_up_time, break_times, seal_ratio, turnover_rate,
                        volume_bias, flow_capital, total_capital, concept, reason)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (r["date"], r["code"], r["name"], r["price"], r["change_percent"],
                     r["limit_up_days"], r["first_limit_up_time"], r["break_times"],
                     r["seal_ratio"], r["turnover_rate"], r["volume_bias"],
                     r["flow_capital"], r["total_capital"], r["concept"], r["reason"]))
            except Exception as e:
                logger.error(f"保存记录失败: {e}")
        try:
            self.db.conn.commit()
        except Exception as e:
            logger.error(f"提交数据失败: {e}")

    def fetch_concept_data(self, date_str):
        """
        获取概念板块数据（选股宝）—— 使用realtime_fetcher代替，此方法保留但不实现
        返回0
        """
        logger.warning("fetch_concept_data 已废弃，请使用 realtime_fetcher 获取概念数据")
        return 0

    def fetch_and_store(self, date_str):
        """
        完整的数据获取流程：仅获取涨停基础数据，概念数据由realtime_fetcher或定时任务补充
        """
        logger.info(f"=== 开始获取 {date_str} 数据 ===")
        limit_count = self.fetch_daily_limit_up(date_str)
        logger.info(f"数据获取完成: 涨停{limit_count}条")
        return limit_count