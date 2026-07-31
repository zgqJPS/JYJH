"""
config_v2.py - 配置管理模块（V2版）
基于67天深度分析结论重构配置
新增配置项：周期模型、回测、信号权重、转移矩阵、推荐参数
"""
import os

# ============ 数据库路径 ============
DB_PATH = "/app/data/所有对话/主对话/stock_data_1784791326780_0_09ym.db"

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
        "strong": {"min_seal_amount": 5.0, "max_turnover": 5.0},
        "medium": {"min_seal_amount": 2.0, "max_turnover": 15.0},
        "weak": {"min_seal_amount": 0.0, "max_turnover": 100.0},
    },
    # 情绪指标权重
    "sentiment_weights": {
        "limit_up_count": 0.25,
        "max_continuous_boards": 0.20,
        "seal_amount_ratio": 0.15,
        "continuation_rate": 0.20,
        "concept_concentration": 0.20,
    },
    # 概念热度阈值
    "concept_hot_threshold": 5,
    "concept_emerging_threshold": 3,
    # V2新增：日均涨停基准值
    "daily_avg_limit_up": 57.7,  # 67天统计均值
}

# ============ 周期模型配置（V2新增） ============
CYCLE_MODEL_CONFIG = {
    # 4阶段定义
    "phases": {
        "ice": "冰点酝酿期",
        "rise": "蓄力爬升期",
        "boom": "爆发高潮期",
        "crash": "崩塌退潮期",
    },
    # 阶段判断阈值
    "ice_thresholds": {
        "max_boards": 3,        # mb≤3
        "smash_coefficient": 2,  # sc<2
        "limit_up_range": (30, 55),  # lu=30~55
    },
    "rise_thresholds": {
        "max_boards_range": (4, 5),
        "smash_coefficient_range": (2, 4),
        "limit_up_range": (40, 60),
    },
    "boom_thresholds": {
        "max_boards": 6,         # mb≥6
        "smash_coefficient": 4,  # sc>4
        "limit_up_count": 70,    # lu≥70
    },
    "crash_indicators": {
        "mb_drop_threshold": -2,   # mb骤降超过2
        "sc_drop_threshold": -2,   # sc骤降超过2
        "high_to_low": {"from_mb": 6, "to_mb": 4},  # 从高板骤降
    },
    # 平均周期天数
    "avg_cycle_days": 5.8,
}

# ============ 转移概率矩阵（V2核心） ============
TRANSITION_MATRIX = {
    2: {"up": 1.00, "flat": 0.00, "down": 0.00, "avg_next": 3.0},
    3: {"up": 0.82, "flat": 0.18, "down": 0.00, "avg_next": 3.8},
    4: {"up": 0.61, "flat": 0.26, "down": 0.13, "avg_next": 4.4},
    5: {"up": 0.33, "flat": 0.00, "down": 0.67, "avg_next": 4.4},  # 生死线
    6: {"up": 0.83, "flat": 0.00, "down": 0.17, "avg_next": 6.5},  # 加速器
    7: {"up": 0.40, "flat": 0.00, "down": 0.60, "avg_next": 5.8},
    8: {"up": 0.00, "flat": 0.00, "down": 1.00, "avg_next": 4.0},  # 天花板
}

# ============ 5个高价值信号权重（V2新增） ============
SIGNAL_WEIGHTS = {
    1: {
        "name": "5→6突破+砸盘下降",
        "weight": 3,
        "base_limit_up_count": 77.8,
        "success_rate": 1.00,
        "occurrences": 3,
        "description": "连板从5→6且砸盘系数下降，次日涨停数预期77.8",
    },
    2: {
        "name": "砸盘骤降>3+连板≤3",
        "weight": 3,
        "limit_up_adjustment": 30,
        "success_rate": 0.83,
        "occurrences": 6,
        "description": "砸盘系数单日骤降超3点+连板不超3，见底反弹信号",
    },
    3: {
        "name": "连续2天砸盘<3+连板≤3",
        "weight": 2,
        "limit_up_adjustment": 15,
        "success_rate": 0.75,
        "occurrences": 4,
        "description": "连续2天低分歧+低位连板，底部确认信号",
    },
    4: {
        "name": "7板+砸盘>6",
        "weight": 2,
        "limit_up_adjustment": -25,
        "success_rate": 1.00,
        "occurrences": 2,
        "description": "连板7+砸盘>6，见顶崩塌信号",
    },
    5: {
        "name": "4板+涨停<35+砸盘<3",
        "weight": 1,
        "limit_up_adjustment": -10,
        "success_rate": 0.67,
        "occurrences": 3,
        "description": "连板仅4+涨停不足35+低分歧，假突破预警",
    },
}

# ============ 回测配置（V2新增） ============
BACKTEST_CONFIG = {
    # 回测数据范围
    "start_date": None,  # None表示从最早数据开始
    "end_date": None,    # None表示到最新数据结束
    # 回测指标
    "metrics": [
        "prediction_accuracy",       # 预测准确率
        "signal_precision",          # 信号精确度
        "signal_recall",             # 信号召回率
        "cycle_detection_accuracy",  # 周期识别准确率
        "limit_up_count_rmse",       # 涨停数RMSE
        "max_boards_mae",            # 连板高度MAE
    ],
    # 回测输出
    "output_format": "detailed",  # "summary" 或 "detailed"
    "save_predictions": True,      # 是否保存每日预测结果
    "save_signals": True,          # 是否保存信号触发记录
}

# ============ 预测参数 ============
PREDICT_CONFIG = {
    "prediction_types": [
        "limit_up_count",
        "max_continuous_boards",
        "main_concept",
        "sentiment_direction",
        "smash_prediction",
        "operation_advice",
    ],
    "default_confidence": 0.5,
    "min_sample_days": 5,
    "recency_weight": 0.6,
    # V2新增：基准涨停数
    "base_limit_up_count": 57.7,
    # V2新增：使用转移概率矩阵
    "use_transition_matrix": True,
    # V2新增：使用信号修正
    "use_signal_correction": True,
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

# ============ 砸盘系数配置（V2版-分歧度重定义） ============
SMASH_CONFIG = {
    # V2版：5档分歧度分类（替代原有2档）
    "divergence_levels": {
        "极低分歧": (0, 1.5),
        "低分歧": (1.5, 3.0),
        "中等分歧": (3.0, 5.0),
        "高分歧": (5.0, 7.0),
        "极高分歧": (7.0, 999),
    },
    # 兼容旧版阈值（向后兼容）
    "low_pressure_threshold": 3.0,    # 改为3.0（原4.0，对应5档中的低分歧上界）
    "high_pressure_threshold": 7.0,   # 保持7.0（对应高分歧上界）
    "climax_threshold": 4.5,
    "main_rise_threshold": 3.0,
    # 预测权重
    "prediction_weight": 0.35,
    # 个股评分调整（基于5档）
    "score_adjustments": {
        "极低分歧": 3,
        "低分歧": 1,
        "中等分歧": -1,
        "高分歧": -3,
        "极高分歧": -5,
    },
    # 操作建议
    "stop_loss_extension": 2.0,
    "max_board_level": 10,
}

# ============ 推荐参数（V2新增） ============
STOCK_RECOMMENDER_CONFIG = {
    # 推荐策略
    "strategy": "signal_weighted",  # 基于信号权重的推荐策略
    # 选股因子权重
    "factor_weights": {
        "continuous_boards": 0.20,      # 连板高度权重
        "seal_quality": 0.15,           # 封板质量权重
        "concept_heat": 0.20,           # 概念热度权重
        "market_position": 0.15,        # 市场地位权重
        "smash_divergence": 0.15,       # 分歧度影响权重
        "continuation_rate": 0.15,      # 晋级率权重
    },
    # 推荐数量
    "max_recommendations": 10,
    "min_recommendations": 3,
    # 过滤条件
    "filters": {
        "min_seal_amount": 0.5,    # 最低封单额（亿）
        "max_turnover": 50.0,      # 最高换手率
        "min_market_cap": 20.0,    # 最低市值（亿）
        "exclude_st": True,        # 排除ST
        "exclude_new_stock_days": 0,  # 排除新股天数
    },
    # 信号触发时的推荐调整
    "signal_adjustments": {
        1: {"position_boost": 0.3, "description": "信号1触发→龙头加分"},
        2: {"new_stock_bonus": 2, "description": "信号2触发→新增2只低位推荐"},
        3: {"new_stock_bonus": 3, "description": "信号3触发→新增3只低位推荐"},
        4: {"reduce_count": True, "description": "信号4触发→减少推荐数量"},
        5: {"conservative_mode": True, "description": "信号5触发→保守模式"},
    },
}

# ============ 日志配置 ============
LOG_CONFIG = {
    "level": "INFO",
    "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    "date_format": "%Y-%m-%d %H:%M:%S",
}
