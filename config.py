"""
config.py - 配置管理模块
管理数据库路径、分析参数、预测参数等配置
"""
import os

# 数据库路径
_db_candidates = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "stock_data_1784791326780_0_09ym.db"),
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "stock_data_1784791326780_0_09ym.db"),
]
DB_PATH = None
for _p in _db_candidates:
    if os.path.exists(_p):
        DB_PATH = _p
        break
if DB_PATH is None:
    DB_PATH = _db_candidates[0]

# 知识库目录
KNOWLEDGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge")

# ============ 数据过滤配置 ============
DATA_FILTER_CONFIG = {
    # 主板代码前缀（沪市60，深市00）
    "main_board_prefixes": ("60", "00"),
    # 需要排除的代码前缀（科创板、创业板、北交所等）
    "excluded_prefixes": ("30", "68", "8", "400", "420", "430", "830"),
    # ST股关键词
    "st_keywords": ("ST", "*ST"),
}

# ============ 市场分析参数 ============
ANALYSIS_CONFIG = {
    "board_tiers": {
        "首板": 1,
        "二板": 2,
        "三板": 3,
        "高标_4_6": (4, 6),
        "超高标_7plus": 7,
    },
    "seal_quality": {
        "strong": {"min_seal_amount": 5.0, "max_turnover": 5.0},
        "medium": {"min_seal_amount": 2.0, "max_turnover": 15.0},
        "weak": {"min_seal_amount": 0.0, "max_turnover": 100.0},
    },
    "sentiment_weights": {
        "limit_up_count": 0.25,
        "max_continuous_boards": 0.20,
        "seal_amount_ratio": 0.15,
        "continuation_rate": 0.20,
        "concept_concentration": 0.20,
    },
    "concept_hot_threshold": 5,
    "concept_emerging_threshold": 3,
}

# ============ 预测参数 ============
PREDICT_CONFIG = {
    "prediction_types": [
        "limit_up_count",
        "max_continuous_boards",
        "main_concept",
        "sentiment_direction",
        "operation_advice",
    ],
    "default_confidence": 0.5,
    "min_sample_days": 5,
    "recency_weight": 0.6,
}

# ============ 自我修正参数 ============
CORRECTION_CONFIG = {
    "ewma_alpha": 0.3,
    "adjustment_step": 0.05,
    "weight_min": 0.05,
    "weight_max": 0.95,
    "default_weight": 0.5,
    "low_confidence_threshold": 3,
    "knowledge_decay_days": 15,
    "knowledge_decay_factor": 0.95,
}

# ============ 砸盘系数配置 ============
SMASH_CONFIG = {
    # 砸盘系数阈值（统一从这里读取）
    "low_pressure_threshold": 4.0,      # 低于此值：抛压轻
    "high_pressure_threshold": 7.0,     # 高于此值：抛压重
    "climax_threshold": 4.5,            # 高潮期判断用
    "main_rise_threshold": 3.0,         # 主升期判断用
    "prediction_weight": 0.35,          # 砸盘系数在预测中的基础权重
    "score_penalty_high": -5,           # 抛压重时的扣分
    "score_bonus_low": 3,               # 抛压轻时的加分
    "stop_loss_extension": 2.0,         # 抛压重时止损放宽百分比
    "max_board_level": 10,              # 最大连板级别
    # 趋势判断阈值
    "trend_rise_threshold": 1.0,        # 上升趋势判定阈值
    "trend_fall_threshold": -1.0,       # 下降趋势判定阈值
    # 信号判断阈值
    "signal_high_risk": 6.0,            # 高风险阈值
    "signal_medium_risk": 4.0,          # 中等风险阈值
    "signal_low_risk": 2.0,             # 低风险阈值
}

# ============ 日志配置 ============
LOG_CONFIG = {
    "level": "INFO",
    "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    "date_format": "%Y-%m-%d %H:%M:%S",
}