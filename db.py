"""
db.py - 数据库操作封装
封装SQLite操作，初始化新表
"""
import sqlite3
import logging
import json
from datetime import datetime

logger = logging.getLogger(__name__)


class Database:
    """数据库操作封装"""

    def __init__(self, db_path):
        self.db_path = db_path
        self.conn = None
        self._connect()

    def _connect(self):
        """建立数据库连接"""
        try:
            self.conn = sqlite3.connect(self.db_path, isolation_level=None)
            self.conn.row_factory = sqlite3.Row
            logger.info(f"数据库连接成功: {self.db_path}")
        except Exception as e:
            logger.error(f"数据库连接失败: {e}")
            raise

    def close(self):
        """关闭连接"""
        if self.conn:
            self.conn.close()
            logger.info("数据库连接已关闭")

    def execute(self, sql, params=None):
        """执行SQL语句"""
        try:
            cursor = self.conn.cursor()
            if params:
                cursor.execute(sql, params)
            else:
                cursor.execute(sql)
            self.conn.commit()
            return cursor
        except Exception as e:
            logger.error(f"SQL执行失败: {sql}, 错误: {e}")
            self.conn.rollback()
            raise

    def fetch_all(self, sql, params=None):
        """查询所有结果"""
        try:
            cursor = self.conn.cursor()
            if params:
                cursor.execute(sql, params)
            else:
                cursor.execute(sql)
            return cursor.fetchall()
        except Exception as e:
            logger.error(f"查询失败: {sql}, 错误: {e}")
            return []

    def fetch_one(self, sql, params=None):
        """查询单条结果"""
        try:
            cursor = self.conn.cursor()
            if params:
                cursor.execute(sql, params)
            else:
                cursor.execute(sql)
            return cursor.fetchone()
        except Exception as e:
            logger.error(f"查询失败: {sql}, 错误: {e}")
            return None

    def init_new_tables(self):
        """初始化系统所需的新表"""
        tables = [
            # 涨停基础数据表（akshare数据源）
            """CREATE TABLE IF NOT EXISTS akshare_limit_up (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT,
                continuous_boards INTEGER DEFAULT 1,
                seal_amount REAL DEFAULT 0,
                seal_style TEXT DEFAULT '',
                turnover_rate REAL DEFAULT 0,
                latest_price REAL DEFAULT 0,
                change_percent REAL DEFAULT 0,
                UNIQUE(date, code)
            )""",

            # 预测记录表
            """CREATE TABLE IF NOT EXISTS prediction_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                prediction_type TEXT NOT NULL,
                content TEXT NOT NULL,
                confidence REAL DEFAULT 0.5,
                actual_result TEXT,
                accuracy_score REAL,
                verified INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(date, prediction_type)
            )""",

            # 模型权重表
            """CREATE TABLE IF NOT EXISTS model_weights (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                factor_name TEXT NOT NULL UNIQUE,
                weight REAL DEFAULT 0.5,
                history TEXT DEFAULT '[]',
                consecutive_misses INTEGER DEFAULT 0,
                credibility REAL DEFAULT 1.0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",

            # 市场知识库表
            """CREATE TABLE IF NOT EXISTS market_knowledge (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern_type TEXT NOT NULL,
                description TEXT NOT NULL,
                occurrence_count INTEGER DEFAULT 1,
                success_rate REAL DEFAULT 0.5,
                last_verified TEXT,
                last_seen TEXT,
                metadata TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(pattern_type, description)
            )""",

            # 修正日志表
            """CREATE TABLE IF NOT EXISTS correction_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                trigger TEXT NOT NULL,
                factor_name TEXT NOT NULL,
                old_weight REAL,
                new_weight REAL,
                reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",

            # 每日市场快照表（用于回测和统计）
            """CREATE TABLE IF NOT EXISTS daily_snapshot (
                date TEXT PRIMARY KEY,
                limit_up_count INTEGER,
                max_continuous_boards INTEGER,
                avg_seal_amount REAL,
                avg_turnover_rate REAL,
                main_concept TEXT,
                main_concept_count INTEGER,
                sentiment_score REAL,
                cycle_phase TEXT,
                board_distribution TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",

            # 回测记录表
            """CREATE TABLE IF NOT EXISTS backtest_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                backtest_date TEXT NOT NULL,
                target_date TEXT NOT NULL,
                prediction_type TEXT NOT NULL,
                predicted_value TEXT,
                actual_value TEXT,
                accuracy_score REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",

            # 选股宝涨停详情表
            """CREATE TABLE IF NOT EXISTS xgb_limit_up_detail (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT,
                concept TEXT,
                reason TEXT,
                fetch_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(date, code)
            )""",

            # 概念统计表
            """CREATE TABLE IF NOT EXISTS concept_statistics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                concept TEXT NOT NULL,
                count INTEGER,
                UNIQUE(date, concept)
            )""",

            # 砸盘系数结果表（旧，保留兼容）
            """CREATE TABLE IF NOT EXISTS smash_coefficient_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                smash_coefficient REAL,
                max_continuous_boards INTEGER,
                UNIQUE(date)
            )""",

            # ===== 新增：砸盘系数主表（统一数据源） =====
            """CREATE TABLE IF NOT EXISTS smash_coefficients (
                trade_date TEXT PRIMARY KEY,
                smash_coefficient REAL,
                limit_up_count INTEGER,
                max_continuous_days INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
        ]

        cursor = self.conn.cursor()
        success_count = 0
        for sql in tables:
            try:
                cursor.execute(sql)
                success_count += 1
                logger.info(f"表创建/验证成功")
            except Exception as e:
                logger.error(f"建表失败: {e}")
        self.conn.commit()

        # 验证关键表是否存在
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        existing = {row[0] for row in cursor.fetchall()}
        required = ['akshare_limit_up', 'prediction_records', 'model_weights',
                     'market_knowledge', 'correction_log', 'daily_snapshot',
                     'backtest_records', 'xgb_limit_up_detail',
                     'concept_statistics', 'smash_coefficient_results',
                     'smash_coefficients']  # 新增
        missing = [t for t in required if t not in existing]
        if missing:
            logger.error(f"⚠️ 以下表创建后仍不存在: {missing}")
            logger.error(f"当前数据库所有表: {sorted(existing)}")
        else:
            logger.info(f"✅ 所有{len(required)}张表验证通过 (成功创建{success_count}张)")

        logger.info(f"所有新表初始化完成 (数据库: {self.db_path})")

    # ============ 预测记录操作 ============
    def save_prediction(self, date, prediction_type, content, confidence=0.5):
        """保存预测记录"""
        sql = """INSERT OR REPLACE INTO prediction_records 
                 (date, prediction_type, content, confidence, created_at)
                 VALUES (?, ?, ?, ?, ?)"""
        self.execute(sql, (date, prediction_type, content, confidence, datetime.now()))

    def get_unverified_predictions(self):
        """获取未验证的预测"""
        sql = "SELECT * FROM prediction_records WHERE verified = 0 ORDER BY date"
        return self.fetch_all(sql)

    def verify_prediction(self, prediction_id, actual_result, accuracy_score):
        """验证预测并记录结果"""
        sql = """UPDATE prediction_records 
                 SET actual_result = ?, accuracy_score = ?, verified = 1
                 WHERE id = ?"""
        self.execute(sql, (actual_result, accuracy_score, prediction_id))

    def get_prediction_history(self, prediction_type=None, limit=50):
        """获取预测历史"""
        sql = "SELECT * FROM prediction_records"
        params = []
        if prediction_type:
            sql += " WHERE prediction_type = ?"
            params.append(prediction_type)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return self.fetch_all(sql, params)

    # ============ 模型权重操作 ============
    def get_all_weights(self):
        """获取所有权重"""
        return self.fetch_all("SELECT * FROM model_weights")

    def get_weight(self, factor_name):
        """获取某个因素的权重"""
        return self.fetch_one(
            "SELECT * FROM model_weights WHERE factor_name = ?", (factor_name,))

    def update_weight(self, factor_name, new_weight, reason="", date=""):
        """更新权重"""
        existing = self.get_weight(factor_name)
        if existing:
            old_weight = existing["weight"]
            history = json.loads(existing["history"]) if existing["history"] else []
            history.append({"weight": new_weight, "date": date, "reason": reason})
            if len(history) > 100:
                history = history[-100:]
            self.execute(
                """UPDATE model_weights SET weight = ?, history = ?, updated_at = ?
                   WHERE factor_name = ?""",
                (new_weight, json.dumps(history), datetime.now(), factor_name))
        else:
            history = [{"weight": new_weight, "date": date, "reason": "初始化"}]
            self.execute(
                """INSERT INTO model_weights (factor_name, weight, history, updated_at)
                   VALUES (?, ?, ?, ?)""",
                (factor_name, new_weight, json.dumps(history), datetime.now()))

    def init_default_weights(self):
        """初始化默认权重"""
        default_factors = {
            "momentum_factor": 0.5,
            "continuation_factor": 0.5,
            "concept_heat_factor": 0.5,
            "seal_quality_factor": 0.5,
            "cycle_factor": 0.5,
            "dragon_factor": 0.5,
            "volume_factor": 0.5,
            "breadth_factor": 0.5,
            "smash_factor": 0.7,
        }
        for name, weight in default_factors.items():
            existing = self.get_weight(name)
            if not existing:
                self.update_weight(name, weight, reason="系统初始化", date=datetime.now().strftime("%Y-%m-%d"))
        logger.info("默认权重初始化完成")

    # ============ 知识库操作 ============
    def save_knowledge(self, pattern_type, description, metadata=None):
        """保存知识"""
        existing = self.fetch_one(
            "SELECT * FROM market_knowledge WHERE pattern_type = ? AND description = ?",
            (pattern_type, description))
        if existing:
            self.execute(
                """UPDATE market_knowledge 
                   SET occurrence_count = occurrence_count + 1, last_seen = ?
                   WHERE pattern_type = ? AND description = ?""",
                (datetime.now().strftime("%Y-%m-%d"), pattern_type, description))
        else:
            self.execute(
                """INSERT INTO market_knowledge (pattern_type, description, metadata, last_seen)
                   VALUES (?, ?, ?, ?)""",
                (pattern_type, description, json.dumps(metadata or {}), datetime.now().strftime("%Y-%m-%d")))

    def get_knowledge(self, pattern_type=None):
        """获取知识"""
        if pattern_type:
            return self.fetch_all(
                "SELECT * FROM market_knowledge WHERE pattern_type = ? ORDER BY occurrence_count DESC",
                (pattern_type,))
        return self.fetch_all("SELECT * FROM market_knowledge ORDER BY occurrence_count DESC")

    def update_knowledge_score(self, knowledge_id, success_rate, verified_date):
        """更新知识的成功率"""
        self.execute(
            """UPDATE market_knowledge 
               SET success_rate = ?, last_verified = ?
               WHERE id = ?""",
            (success_rate, verified_date, knowledge_id))

    # ============ 每日快照操作 ============
    def save_daily_snapshot(self, date, data):
        """保存每日市场快照"""
        sql = """INSERT OR REPLACE INTO daily_snapshot 
                 (date, limit_up_count, max_continuous_boards, avg_seal_amount,
                  avg_turnover_rate, main_concept, main_concept_count, 
                  sentiment_score, cycle_phase, board_distribution)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
        self.execute(sql, (
            date, data.get("limit_up_count", 0), data.get("max_continuous_boards", 0),
            data.get("avg_seal_amount", 0), data.get("avg_turnover_rate", 0),
            data.get("main_concept", ""), data.get("main_concept_count", 0),
            data.get("sentiment_score", 0), data.get("cycle_phase", ""),
            json.dumps(data.get("board_distribution", {}))
        ))

    def get_daily_snapshots(self, start_date=None, end_date=None, limit=100):
        """获取每日快照"""
        sql = "SELECT * FROM daily_snapshot"
        conditions = []
        params = []
        if start_date:
            conditions.append("date >= ?")
            params.append(start_date)
        if end_date:
            conditions.append("date <= ?")
            params.append(end_date)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY date DESC LIMIT ?"
        params.append(limit)
        return self.fetch_all(sql, params)

    # ============ 涨停数据查询 ============
    def get_limit_up_data(self, date):
        """获取某日涨停数据"""
        return self.fetch_all(
            "SELECT * FROM akshare_limit_up WHERE date = ?", (date,))

    def get_all_dates(self):
        """获取所有有数据的日期"""
        rows = self.fetch_all(
            "SELECT DISTINCT date FROM akshare_limit_up ORDER BY date")
        return [r["date"] for r in rows]

    def get_concept_data(self, date):
        """获取某日概念数据（从concept_statistics表）"""
        return self.fetch_all(
            "SELECT * FROM concept_statistics WHERE date = ?", (date,))

    def get_cycle_context(self, date):
        """获取某日周期上下文"""
        return self.fetch_one(
            "SELECT * FROM cycle_context WHERE date = ?", (date,))

    def get_limit_up_with_concepts(self, date):
        """获取含概念的涨停数据"""
        return self.fetch_all(
            """SELECT l.*, x.concept, x.reason 
               FROM akshare_limit_up l
               LEFT JOIN xgb_limit_up_detail x ON l.date = x.date AND l.code = x.code
               WHERE l.date = ?""", (date,))

    # ============ 选股宝涨停详情操作 ============
    def save_xgb_detail(self, records, date):
        """批量保存选股宝涨停详情数据"""
        count = 0
        for r in records:
            try:
                self.execute(
                    """INSERT OR REPLACE INTO xgb_limit_up_detail 
                       (date, code, name, concept, reason)
                       VALUES (?, ?, ?, ?, ?)""",
                    (date, r["code"], r.get("name", ""),
                     r.get("concept", ""), r.get("reason", "")))
                count += 1
            except Exception as e:
                logger.error(f"保存选股宝记录失败: {e}")
        self.conn.commit()
        logger.info(f"选股宝详情数据保存完成: {count}/{len(records)} 条")
        return count

    def get_xgb_detail(self, date):
        """获取某日选股宝涨停详情数据"""
        return self.fetch_all(
            "SELECT * FROM xgb_limit_up_detail WHERE date = ?", (date,))

    def get_xgb_concepts_by_date(self, date):
        """从xgb_limit_up_detail表聚合概念统计"""
        rows = self.fetch_all(
            "SELECT concept FROM xgb_limit_up_detail WHERE date = ? AND concept IS NOT NULL AND concept != ''",
            (date,))
        concept_counter = {}
        for row in rows:
            concepts = row["concept"]
            if not concepts:
                continue
            for c in concepts.split(";"):
                c = c.strip()
                if c and "ST" not in c:
                    concept_counter[c] = concept_counter.get(c, 0) + 1
        return concept_counter

    def get_concept_statistics(self, date):
        """获取concept_statistics表数据"""
        return self.fetch_all(
            "SELECT * FROM concept_statistics WHERE date = ? ORDER BY count DESC", (date,))

    def save_concept_statistics(self, records, date):
        """保存概念统计数据"""
        count = 0
        for r in records:
            try:
                self.execute(
                    """INSERT OR REPLACE INTO concept_statistics 
                       (date, concept, count)
                       VALUES (?, ?, ?)""",
                    (date, r["concept"], r["count"]))
                count += 1
            except Exception as e:
                logger.error(f"保存概念统计失败: {e}")
        self.conn.commit()
        logger.info(f"概念统计数据保存完成: {count}/{len(records)} 条")
        return count

    # ============ 砸盘系数操作 ============
    def save_smash_coefficient(self, date, coefficient, max_boards):
        """
        保存砸盘系数结果（写入统一表 smash_coefficients）
        同时保留旧表写入以便兼容，但走势图从新表读取
        """
        # 写入新表
        self.execute(
            """INSERT OR REPLACE INTO smash_coefficients 
               (trade_date, smash_coefficient, max_continuous_days)
               VALUES (?, ?, ?)""",
            (date, coefficient, max_boards))
        # 同时写入旧表（兼容）
        self.execute(
            """INSERT OR REPLACE INTO smash_coefficient_results 
               (date, smash_coefficient, max_continuous_boards)
               VALUES (?, ?, ?)""",
            (date, coefficient, max_boards))

    def get_smash_coefficient(self, date):
        """获取单日砸盘系数（优先从新表读取）"""
        row = self.fetch_one(
            "SELECT smash_coefficient FROM smash_coefficients WHERE trade_date = ?", (date,))
        if row:
            return {"smash_coefficient": row["smash_coefficient"]}
        # 降级到旧表
        return self.fetch_one(
            "SELECT * FROM smash_coefficient_results WHERE date = ?", (date,))

    def get_smash_coefficient_history(self, start_date=None, end_date=None, limit=30):
        """
        获取砸盘系数历史数据（从统一表 smash_coefficients 读取）
        返回字段：date, smash_coefficient, max_continuous_boards
        """
        sql = """
            SELECT trade_date as date, 
                   smash_coefficient, 
                   max_continuous_days as max_continuous_boards
            FROM smash_coefficients
        """
        conditions = []
        params = []
        if start_date:
            conditions.append("trade_date >= ?")
            params.append(start_date)
        if end_date:
            conditions.append("trade_date <= ?")
            params.append(end_date)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY trade_date DESC LIMIT ?"
        params.append(limit)
        return self.fetch_all(sql, params)