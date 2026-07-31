"""
config.py - 配置管理模块
管理数据库路径、分析参数、预测参数等配置
"""
import os

# 数据库路径
# 数据库路径 - 自动检测（兼容多环境）
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

# ============ 市场分析参数 ============
ANALYSIS_CONFIG = {
    # 连板梯队阈值
    "board_tiers": {
        "首板": 1,
        "二板": 2,
        "三板": 3,
        "高标_4_6": (4, 6),
        "超高标_7plus": 7,
    },
    # 封板质量评级
    "seal_quality": {
        "strong": {"min_seal_amount": 5.0, "max_turnover": 5.0},   # 强封：封单>5亿，换手<5%
        "medium": {"min_seal_amount": 2.0, "max_turnover": 15.0},  # 中封
        "weak": {"min_seal_amount": 0.0, "max_turnover": 100.0},   # 弱封
    },
    # 情绪指标权重
    "sentiment_weights": {
        "limit_up_count": 0.25,        # 涨停数量
        "max_continuous_boards": 0.20,  # 最高连板
        "seal_amount_ratio": 0.15,      # 封单额比率
        "continuation_rate": 0.20,      # 晋级率
        "concept_concentration": 0.20,  # 概念集中度
    },
    # 概念热度阈值
    "concept_hot_threshold": 5,    # 涨停数>=5视为热门概念
    "concept_emerging_threshold": 3,  # >=3视为新兴概念
}

# ============ 预测参数 ============
PREDICT_CONFIG = {
    # 预测类型
    "prediction_types": [
        "limit_up_count",         # 涨停数量预测
        "max_continuous_boards",  # 最高连板预测
        "main_concept",           # 主线概念预测
        "sentiment_direction",    # 情绪方向预测
        "operation_advice",       # 操作建议
    ],
    # 默认置信度
    "default_confidence": 0.5,
    # 最低样本数（历史数据至少需要这么多天才能生成预测）
    "min_sample_days": 5,
    # 近期权重（越近的数据权重越高）
    "recency_weight": 0.6,
}

# ============ 自我修正参数 ============
CORRECTION_CONFIG = {
    # EWMA衰减因子（越大越重视近期数据）
    "ewma_alpha": 0.3,
    # 权重调整步长
    "adjustment_step": 0.05,
    # 权重上下限
    "weight_min": 0.05,
    "weight_max": 0.95,
    # 初始权重
    "default_weight": 0.5,
    # 连续误判阈值（超过此值标记为低可信度）
    "low_confidence_threshold": 3,
    # 知识衰减天数（超过此天数未验证则降低权重）
    "knowledge_decay_days": 15,
    # 知识衰减因子
    "knowledge_decay_factor": 0.95,
}

# ============ 砸盘系数配置 ============
SMASH_CONFIG = {
    # 砸盘系数阈值（可被self_corrector动态调整）
    "low_pressure_threshold": 4.0,    # 低于此值：抛压轻
    "high_pressure_threshold": 7.0,   # 高于此值：抛压重
    "climax_threshold": 4.5,          # 高潮期判断用
    "main_rise_threshold": 3.0,       # 主升期判断用
    # 砸盘系数在预测中的权重（初始值，可自适应调整）
    "prediction_weight": 0.35,        # 砸盘系数在预测中的基础权重（最高）
    # 个股评分调整
    "score_penalty_high": -5,         # 抛压重时的扣分
    "score_bonus_low": 3,             # 抛压轻时的加分
    # 操作建议相关
    "stop_loss_extension": 2.0,       # 抛压重时止损放宽百分比
    # 板级计算范围
    "max_board_level": 10,            # 最大连板级别
}

# ============ 日志配置 ============
LOG_CONFIG = {
    "level": "INFO",
    "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    "date_format": "%Y-%m-%d %H:%M:%S",
}
