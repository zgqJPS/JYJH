"""
operation_planner.py - 确定性龙头操作计划引擎
==============================================
基于龙头识别结果，制定完整的操作计划，包括：
  - 买入策略（打板/低吸/半路）+ 具体价位区间 + 时机条件
  - 仓位决策矩阵（周期阶段 × 砸盘系数 × 确定性等级）
  - 止损止盈规则（动态止损，基于板级和封板质量）
  - 场景推演（最好/基准/最坏三种情况及应对）
  - 持仓管理（加仓/减仓/清仓条件）

设计原则：
  - 胜率第一：只有高确定性才给重仓计划，低确定性宁可不操作
  - 收益第二：在胜率保障的前提下，通过仓位和止盈优化收益
  - 风控前置：每个计划都先算止损，再算收益，盈亏比不达标不出计划
  - 可执行：价位、仓位、条件全部具体化，不输出模糊建议

用法:
  from operation_planner import OperationPlanner
  planner = OperationPlanner(db_path)
  plans = planner.generate_plans('2026-08-12')
  report = planner.format_plans(plans)
"""

import sqlite3
import json
import logging
import math
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime

from config import DB_PATH
from dragon_detector import DragonDetector, DRAGON_TYPES, LIFECYCLE_STAGES, CERTAINTY_LEVELS

try:
    from board_calculator import BoardCalculator
    _HAS_BOARD_CALC = True
except ImportError:
    _HAS_BOARD_CALC = False

logger = logging.getLogger('operation_planner')

# ─────────────────────────── 仓位决策矩阵 ───────────────────────────
# 行：确定性等级（SS/S/A/B）
# 列：市场环境（基于砸盘系数+周期综合判断）
# 值：建议仓位占总资金比例

POSITION_MATRIX = {
    # SS级龙头：极高确定性，可重仓
    'SS': {
        'friendly':   0.30,   # 友好市场（砸盘<3）
        'neutral':    0.25,   # 中性市场（砸盘3-5）
        'cautious':   0.15,   # 谨慎市场（砸盘5-7）
        'hostile':    0.00,   # 恶劣市场（砸盘>=7）：不操作
    },
    # S级龙头：高确定性
    'S': {
        'friendly':   0.20,
        'neutral':    0.15,
        'cautious':   0.10,
        'hostile':    0.00,
    },
    # A级龙头：较高确定性
    'A': {
        'friendly':   0.15,
        'neutral':    0.10,
        'cautious':   0.05,
        'hostile':    0.00,
    },
    # B级龙头：中等确定性，轻仓试错
    'B': {
        'friendly':   0.08,
        'neutral':    0.05,
        'cautious':   0.00,
        'hostile':    0.00,
    },
}

# 最大总仓位限制（不论多少只股票）
MAX_TOTAL_POSITION = {
    'friendly': 0.60,
    'neutral':  0.45,
    'cautious': 0.25,
    'hostile':  0.05,   # 恶劣市场最多5%试水
}

# ─────────────────────────── 买入策略定义 ───────────────────────────

BUY_STRATEGIES = {
    'board_hit': {
        'name': '打板',
        'desc': '涨停价挂单买入，追求确定性封板',
        'icon': '🎯',
    },
    'half_way': {
        'name': '半路',
        'desc': '涨幅5%-8%区间低吸，博弈涨停',
        'icon': '🛤️',
    },
    'low_buy': {
        'name': '低吸',
        'desc': '回调至关键支撑位买入，成本更低但确定性降低',
        'icon': '💰',
    },
    'wait': {
        'name': '观望',
        'desc': '当前无合适买点，等待条件触发',
        'icon': '⏸️',
    },
}

# ─────────────────────────── 止损参数 ───────────────────────────

STOP_LOSS_CONFIG = {
    'board_hit': {
        # 打板买入：次日不板即走，最大容忍跌幅
        'max_loss_pct': -3.0,        # 最大亏损3%
        'next_day_no_board': True,    # 次日未涨停则止损
        'break_seal_exit': True,      # 盘中破板且无法回封则止损
    },
    'half_way': {
        'max_loss_pct': -5.0,
        'next_day_no_board': False,
        'break_seal_exit': False,
    },
    'low_buy': {
        'max_loss_pct': -6.0,
        'next_day_no_board': False,
        'break_seal_exit': False,
    },
}

# 涨停幅度映射
LIMIT_UP_PCT = {
    'main_board': 0.10,    # 主板10%
    'gem_star':   0.20,    # 创业板/科创板20%
    'bse':        0.30,    # 北交所30%
}


class OperationPlanner:
    """确定性龙头操作计划引擎"""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self.detector = DragonDetector(db_path)
        self._board_calc = None
        if _HAS_BOARD_CALC:
            try:
                conn = sqlite3.connect(db_path)
                self._board_calc = BoardCalculator(conn)
            except Exception as e:
                logger.warning(f"BoardCalculator初始化失败: {e}")

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _get_smash_coefficient(self, date: str) -> Optional[float]:
        conn = self._get_conn()
        try:
            row = conn.execute("""
                SELECT smash_coefficient FROM smash_coefficients
                WHERE trade_date = ?
            """, (date,)).fetchone()
            if row and row['smash_coefficient'] is not None:
                return row['smash_coefficient']
            row = conn.execute("""
                SELECT smash_coefficient FROM smash_coefficients
                WHERE trade_date < ? AND smash_coefficient IS NOT NULL
                ORDER BY trade_date DESC LIMIT 1
            """, (date,)).fetchone()
            return row['smash_coefficient'] if row else None
        finally:
            conn.close()

    def _get_daily_summary(self, date: str) -> Optional[Dict]:
        conn = self._get_conn()
        try:
            row = conn.execute("""
                SELECT * FROM xgt_daily_summary WHERE date = ?
            """, (date,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def _get_stock_price(self, code: str, date: str) -> Optional[Dict]:
        """获取个股最新价格数据"""
        conn = self._get_conn()
        try:
            row = conn.execute("""
                SELECT code, name, price, change_percent, limit_up_days,
                       seal_ratio, turnover_rate, volume_bias, flow_capital,
                       first_limit_up_time, break_times
                FROM xgt_limit_up_detail
                WHERE code = ? AND date = ?
            """, (code, date)).fetchone()
            if not row:
                return None
            d = dict(row)
            if self._board_calc:
                try:
                    real = self._board_calc.get_consecutive_boards(date, code, conn)
                    if real > 0:
                        d['api_limit_up_days'] = d.get('limit_up_days', 1)
                        d['limit_up_days'] = real
                except Exception:
                    pass
            return d
        finally:
            conn.close()

    # ─────────────────────────── 市场环境判断 ───────────────────────────

    def _assess_market_env(self, date: str, smash: Optional[float],
                            summary: Optional[Dict]) -> Dict[str, Any]:
        """
        综合判断市场环境，返回 friendly/neutral/cautious/hostile
        """
        explosion_rate = (summary.get('explosion_rate', 0) if summary else 0) or 0
        limit_up_count = summary.get('limit_up_count', 0) if summary else 0

        # 基于砸盘系数判断
        if smash is not None:
            if smash < 3.0:
                env = 'friendly'
            elif smash < 5.0:
                env = 'neutral'
            elif smash < 7.0:
                env = 'cautious'
            else:
                env = 'hostile'
        else:
            env = 'neutral'

        # 炸板率修正
        if explosion_rate > 0.35:
            # 炸板率极高，降一级
            env_order = ['friendly', 'neutral', 'cautious', 'hostile']
            idx = env_order.index(env)
            env = env_order[min(idx + 1, 3)]
        elif explosion_rate < 0.10 and smash is not None and smash < 5.0:
            # 炸板率极低，升一级
            env_order = ['friendly', 'neutral', 'cautious', 'hostile']
            idx = env_order.index(env)
            env = env_order[max(idx - 1, 0)]

        env_names = {
            'friendly': '友好（适合进攻）',
            'neutral':  '中性（常规操作）',
            'cautious': '谨慎（控制仓位）',
            'hostile':  '恶劣（空仓观望）',
        }

        return {
            'env': env,
            'name': env_names[env],
            'smash': smash,
            'explosion_rate': explosion_rate,
            'limit_up_count': limit_up_count,
        }

    # ─────────────────────────── 买入策略选择 ───────────────────────────

    def _determine_buy_strategy(self, dragon: Dict, market_env: Dict) -> Dict[str, Any]:
        """
        根据龙头特征和市场环境决定买入策略。

        决策逻辑：
        1. 衰退期龙头 → 观望
        2. SS/S级 + 启动/加速期 + 封板强 → 打板（次日竞价/开盘）
        3. A级 + 封板强 + 友好/中性市场 → 打板或半路
        4. B级或封板弱 → 低吸（等回调）
        5. 恶劣市场 → 一律观望
        """
        lifecycle = dragon['lifecycle_stage']
        certainty = dragon['certainty_level']
        seal_ratio = dragon.get('seal_ratio') or 0
        break_times = dragon.get('break_times') or 0
        boards = dragon.get('limit_up_days', 1) or 1
        dragon_type = dragon['dragon_type']
        env = market_env['env']
        smash = market_env.get('smash')

        strategy = 'wait'
        conditions = []
        price_range = {}

        # 恶劣市场一律观望
        if env == 'hostile':
            return {
                'strategy': 'wait',
                'strategy_name': BUY_STRATEGIES['wait']['name'],
                'reason': f"砸盘系数{smash:.1f}极高，市场环境恶劣，空仓观望",
                'conditions': ['等待砸盘系数回落至5以下再考虑'],
                'price_range': {},
                'timing': '无',
            }

        # 衰退期不操作
        if lifecycle == 'decline':
            return {
                'strategy': 'wait',
                'strategy_name': BUY_STRATEGIES['wait']['name'],
                'reason': '龙头已进入衰退期，盈亏比恶化',
                'conditions': ['不参与衰退期龙头', '等待新周期启动'],
                'price_range': {},
                'timing': '无',
            }

        # 获取涨停价和前收盘价
        price = dragon.get('price', 0) or 0
        limit_pct = self._get_limit_pct(dragon['code'])
        prev_close = price / (1 + limit_pct) if price > 0 else 0

        # 决策树
        if certainty in ('SS', 'S') and lifecycle in ('launch', 'acceleration'):
            if seal_ratio >= 0.05 and break_times == 0:
                strategy = 'board_hit'
                conditions.append(f"次日集合竞价高开3%-7%区间挂单")
                conditions.append(f"若一字板开盘则排队不撤")
                conditions.append(f"开盘后5分钟内必须封板，否则撤单")
                if boards >= 4:
                    conditions.append("高标股注意分歧，封单不足不追")
            elif seal_ratio >= 0.02:
                strategy = 'half_way'
                conditions.append("次日开盘观察15分钟")
                conditions.append(f"回落至{prev_close * 1.03:.2f}-{prev_close * 1.06:.2f}区间可低吸")
                conditions.append("跌破分时均线不买")
            else:
                strategy = 'low_buy'
                conditions.append("封板质量偏弱，等回调")
                conditions.append(f"回调至5日均线附近（约{prev_close * 0.97:.2f}）考虑")
        elif certainty == 'A':
            if seal_ratio >= 0.05 and break_times == 0 and env in ('friendly', 'neutral'):
                strategy = 'board_hit'
                conditions.append("次日竞价高开不超过5%时挂单")
                conditions.append("开盘10分钟内封板才有效")
            elif seal_ratio >= 0.02:
                strategy = 'half_way'
                conditions.append("次日开盘观察30分钟")
                conditions.append(f"涨幅3%-6%区间（{prev_close * 1.03:.2f}-{prev_close * 1.06:.2f}）介入")
                conditions.append("需放量上攻确认")
            else:
                strategy = 'low_buy'
                conditions.append(f"等回调至{prev_close * 0.95:.2f}-{prev_close * 0.98:.2f}区间")
        elif certainty == 'B':
            if env == 'friendly' and seal_ratio >= 0.03:
                strategy = 'half_way'
                conditions.append("仅友好市场可轻仓试水")
                conditions.append(f"涨幅2%-5%区间（{prev_close * 1.02:.2f}-{prev_close * 1.05:.2f}）介入")
            else:
                strategy = 'low_buy'
                conditions.append("B级确定性不足，仅低吸不追高")
                conditions.append(f"等回调至{prev_close * 0.93:.2f}-{prev_close * 0.97:.2f}区间")

        # 补涨龙特殊处理：只低吸不打板
        if dragon_type == 'catch_up_dragon' and strategy == 'board_hit':
            strategy = 'half_way'
            conditions.insert(0, "补涨龙不打板，改半路低吸")

        # 计算价位区间
        if strategy == 'board_hit':
            price_range = {
                'buy_price_low': round(price, 2),
                'buy_price_high': round(price, 2),
                'buy_price_desc': f"涨停价{price:.2f}",
            }
        elif strategy == 'half_way':
            price_range = {
                'buy_price_low': round(prev_close * 1.03, 2),
                'buy_price_high': round(prev_close * 1.06, 2),
                'buy_price_desc': f"{prev_close * 1.03:.2f}~{prev_close * 1.06:.2f}（涨幅3%-6%）",
            }
        elif strategy == 'low_buy':
            price_range = {
                'buy_price_low': round(prev_close * 0.93, 2),
                'buy_price_high': round(prev_close * 0.98, 2),
                'buy_price_desc': f"{prev_close * 0.93:.2f}~{prev_close * 0.98:.2f}（回调2%-7%）",
            }

        # 时机描述
        timing_map = {
            'board_hit': '次日9:25集合竞价~9:35',
            'half_way': '次日9:45~10:30',
            'low_buy': '次日10:00~14:30（分时低点）',
            'wait': '无',
        }

        reason_map = {
            'board_hit': f"{certainty}级确定性+封板坚决+{LIFECYCLE_STAGES[lifecycle]['name']}，打板追求确定性",
            'half_way': f"{certainty}级+封板质量{(seal_ratio or 0):.1%}，半路介入平衡成本与确定性",
            'low_buy': f"{certainty}级确定性一般，低吸控制成本",
            'wait': "条件不满足，观望",
        }

        return {
            'strategy': strategy,
            'strategy_name': BUY_STRATEGIES[strategy]['name'],
            'strategy_icon': BUY_STRATEGIES[strategy]['icon'],
            'reason': reason_map[strategy],
            'conditions': conditions,
            'price_range': price_range,
            'timing': timing_map[strategy],
            'prev_close': round(prev_close, 2),
            'limit_price': round(price, 2),
        }

    def _get_limit_pct(self, code: str) -> float:
        """根据股票代码判断涨停幅度"""
        if code.startswith('300') or code.startswith('688'):
            return 0.20
        elif code.startswith('8') or code.startswith('4'):
            return 0.30
        else:
            return 0.10

    # ─────────────────────────── 仓位决策 ───────────────────────────

    def _determine_position(self, dragon: Dict, market_env: Dict,
                             used_position: float,
                             capital_flow_multiplier: float = 1.0) -> Dict[str, Any]:
        """
        基于决策矩阵确定仓位。
        capital_flow_multiplier: 资金流分析给出的仓位系数（0.1~1.5），
            在基础仓位计算后应用，作为资金面的最终调节。胜率第一，上限不超过1.3。
        返回建议仓位金额占比和具体操作建议。
        """
        certainty = dragon['certainty_level']
        env = market_env['env']
        lifecycle = dragon['lifecycle_stage']
        boards = dragon.get('limit_up_days', 1) or 1

        # 基础仓位
        base_pct = POSITION_MATRIX.get(certainty, POSITION_MATRIX['B']).get(env, 0)

        # 总龙头额外权限：谨慎市场也给最小仓位
        dragon_type = dragon.get('dragon_type', '')
        if base_pct == 0 and dragon_type == 'total_dragon' and env == 'cautious':
            base_pct = 0.03  # 总龙头谨慎市场最小3%试水

        # 生命周期调整
        if lifecycle == 'launch':
            base_pct *= 0.8  # 启动期确定性未完全建立
        elif lifecycle == 'acceleration':
            base_pct *= 1.2  # 加速期是最佳参与窗口
            # 上限不超过该等级在友好市场的仓位
            cap = POSITION_MATRIX.get(certainty, {}).get('friendly', 0.30)
            base_pct = min(base_pct, cap)
        elif lifecycle == 'climax':
            base_pct *= 0.6  # 高潮期风险加大
        elif lifecycle == 'decline':
            base_pct = 0

        # 高板减仓
        if boards >= 6:
            base_pct *= 0.5
        elif boards >= 4:
            base_pct *= 0.8

        # 总龙头保底：非恶劣市场，总龙头至少3%仓位（高潮期也保留观察仓）
        if dragon_type == 'total_dragon' and env != 'hostile':
            base_pct = max(base_pct, 0.03)

        # 资金流仓位系数调节（胜率第一：上限1.3，恶劣市场强制压缩）
        cf_mult = max(0.1, min(1.3, capital_flow_multiplier))
        # 砸盘系数恶劣时，资金流系数不能加仓位
        if env == 'hostile':
            cf_mult = min(cf_mult, 0.3)
        elif env == 'cautious':
            cf_mult = min(cf_mult, 1.0)  # 谨慎市场最多不加仓
        base_pct *= cf_mult

        # 检查总仓位限制
        max_total = MAX_TOTAL_POSITION.get(env, 0.20)
        remaining = max(0, max_total - used_position)
        final_pct = min(base_pct, remaining)

        # 四舍五入到2%
        final_pct = math.floor(final_pct * 50) / 50  # 取整到2%
        final_pct = max(0, final_pct)

        position_desc = f"建议仓位{final_pct:.0%}"
        if final_pct == 0:
            position_desc = "不建议开仓"
        elif final_pct <= 0.05:
            position_desc = f"轻仓试错{final_pct:.0%}"
        elif final_pct <= 0.15:
            position_desc = f"标准仓位{final_pct:.0%}"
        else:
            position_desc = f"重仓{final_pct:.0%}"

        # 分批建仓建议
        batch_plan = []
        if final_pct >= 0.10:
            batch_plan.append(f"首批{final_pct/2:.0%}试仓")
            batch_plan.append(f"确认封板后加至{final_pct:.0%}")
        elif final_pct > 0:
            batch_plan.append(f"一次性建仓{final_pct:.0%}")

        return {
            'position_pct': round(final_pct, 4),
            'position_desc': position_desc,
            'base_pct': round(base_pct, 4),
            'remaining_capacity': round(remaining, 4),
            'max_total_limit': round(max_total, 4),
            'batch_plan': batch_plan,
        }

    # ─────────────────────────── 止损止盈 ───────────────────────────

    def _determine_stop_loss_take_profit(self, dragon: Dict,
                                          buy_info: Dict) -> Dict[str, Any]:
        """
        制定止损止盈规则。
        胜率优先：止损严格，止盈让利润奔跑。
        """
        boards = dragon.get('limit_up_days', 1) or 1
        seal_ratio = dragon.get('seal_ratio') or 0
        lifecycle = dragon['lifecycle_stage']
        strategy = buy_info['strategy']
        prev_close = buy_info.get('prev_close', 0)
        limit_price = buy_info.get('limit_price', 0)

        # ── 止损 ──
        stop_config = STOP_LOSS_CONFIG.get(strategy, STOP_LOSS_CONFIG['low_buy'])
        max_loss_pct = stop_config['max_loss_pct']

        # 根据板级动态调整止损
        if boards >= 5:
            max_loss_pct = max(max_loss_pct - 1, -2.0)  # 高板止损更紧
        elif boards <= 2:
            max_loss_pct = min(max_loss_pct + 1, -7.0)  # 低位容忍度大

        # 封板弱的止损更紧
        if seal_ratio < 0.02 and boards >= 3:
            max_loss_pct = max(max_loss_pct - 1, -2.0)

        stop_loss_price = round(prev_close * (1 + max_loss_pct / 100), 2) if prev_close > 0 else 0

        stop_rules = []
        if strategy == 'board_hit':
            stop_rules.append("次日开盘10分钟未封板→无条件止损")
            stop_rules.append(f"盘中跌破{stop_loss_price:.2f}（{max_loss_pct:.0f}%）→立即止损")
            if boards >= 3:
                stop_rules.append("封板后开板且5分钟内无法回封→止损")
        elif strategy == 'half_way':
            stop_rules.append(f"跌破买入价{abs(max_loss_pct):.0f}%→止损")
            stop_rules.append(f"止损价{stop_loss_price:.2f}")
            stop_rules.append("尾盘未封板→减半仓")
        else:
            stop_rules.append(f"跌破{stop_loss_price:.2f}（{max_loss_pct:.0f}%）→止损")
            stop_rules.append("次日不能反包→离场")

        # ── 止盈 ──
        take_profit_rules = []
        target_prices = []

        if boards >= 5:
            # 高标：次日不板就走
            tp1 = round(limit_price * 1.05, 2)  # 高开5%
            target_prices.append({'target': round(limit_price, 2), 'desc': '次日涨停价（连板）', 'action': '持有观察'})
            target_prices.append({'target': tp1, 'desc': f'高开{tp1:.2f}不封板', 'action': '立即止盈'})
            take_profit_rules.append("高标股次日不板即走，不格局")
            take_profit_rules.append("连板则继续持有，断板当日收盘前清仓")
        elif boards >= 3:
            tp_board = round(limit_price * 1.10, 2) if dragon['code'].startswith(('60', '00')) else round(limit_price * 1.20, 2)
            target_prices.append({'target': tp_board, 'desc': '次日涨停（晋级）', 'action': '持有'})
            target_prices.append({'target': round(limit_price * 1.03, 2), 'desc': '高开3%+不板', 'action': '止盈减半'})
            take_profit_rules.append("次日涨停则持有至断板")
            take_profit_rules.append("高开不封板→先减半仓锁利")
            take_profit_rules.append("断板日若放量→清仓")
        else:
            tp_board = round(limit_price * 1.10, 2) if dragon['code'].startswith(('60', '00')) else round(limit_price * 1.20, 2)
            target_prices.append({'target': tp_board, 'desc': '次日涨停（2板确认）', 'action': '持有'})
            target_prices.append({'target': round(limit_price * 1.05, 2), 'desc': '冲高5%+回落', 'action': '止盈'})
            take_profit_rules.append("次日涨停则持有看3板")
            take_profit_rules.append("冲高回落破均线→止盈")

        # 盈亏比计算
        buy_price = buy_info.get('price_range', {}).get('buy_price_low', prev_close)
        if buy_price > 0 and stop_loss_price > 0:
            risk = buy_price - stop_loss_price
            reward_target = target_prices[0]['target'] if target_prices else buy_price * 1.10
            reward = reward_target - buy_price
            risk_reward_ratio = round(reward / risk, 2) if risk > 0 else 0
        else:
            risk_reward_ratio = 0

        return {
            'stop_loss_price': stop_loss_price,
            'stop_loss_pct': max_loss_pct,
            'stop_rules': stop_rules,
            'take_profit_targets': target_prices,
            'take_profit_rules': take_profit_rules,
            'risk_reward_ratio': risk_reward_ratio,
        }

    # ─────────────────────────── 场景推演 ───────────────────────────

    def _scenario_analysis(self, dragon: Dict, buy_info: Dict,
                            position_info: Dict) -> Dict[str, Any]:
        """
        三种场景推演：最好/基准/最坏
        """
        boards = dragon.get('limit_up_days', 1) or 1
        lifecycle = dragon['lifecycle_stage']
        certainty = dragon['certainty_level']
        seal_ratio = dragon.get('seal_ratio') or 0
        position_pct = position_info['position_pct']
        limit_price = buy_info.get('limit_price', 0)

        limit_pct = self._get_limit_pct(dragon['code'])

        # 基准概率（受确定性和生命周期影响）
        base_prob = {'SS': 0.75, 'S': 0.65, 'A': 0.50, 'B': 0.35}.get(certainty, 0.35)
        if lifecycle == 'acceleration':
            base_prob += 0.10
        elif lifecycle == 'climax':
            base_prob -= 0.15
        elif lifecycle == 'decline':
            base_prob -= 0.30
        base_prob = max(0.10, min(0.85, base_prob))

        # 最好场景：连板晋级
        best_return = limit_pct * 100  # 次日涨停
        best_prob = round(base_prob, 2)
        best_action = "持有，断板再走"

        # 基准场景：高开冲高
        neutral_return = 3.0  # 平均3%收益
        neutral_prob = round(1 - base_prob - 0.15, 2)
        neutral_prob = max(0.10, neutral_prob)
        neutral_action = "冲高不板减半仓"

        # 最坏场景：低开/炸板止损
        worst_return = -abs(STOP_LOSS_CONFIG.get(
            buy_info['strategy'], STOP_LOSS_CONFIG['low_buy'])['max_loss_pct'])
        worst_prob = round(1 - best_prob - neutral_prob, 2)
        worst_prob = max(0.05, worst_prob)
        worst_action = "触发止损立即离场"

        # 重新归一化
        total = best_prob + neutral_prob + worst_prob
        if total > 0:
            best_prob = round(best_prob / total, 2)
            neutral_prob = round(neutral_prob / total, 2)
            worst_prob = round(1 - best_prob - neutral_prob, 2)

        # 期望收益
        expected_return = round(
            best_return * best_prob +
            neutral_return * neutral_prob +
            worst_return * worst_prob, 2
        )

        return {
            'best_case': {
                'scenario': f"次日涨停（{boards+1}板）",
                'return_pct': round(best_return, 1),
                'probability': best_prob,
                'action': best_action,
            },
            'neutral_case': {
                'scenario': "高开3%-5%未封板",
                'return_pct': round(neutral_return, 1),
                'probability': neutral_prob,
                'action': neutral_action,
            },
            'worst_case': {
                'scenario': "低开/炸板/冲高回落",
                'return_pct': round(worst_return, 1),
                'probability': worst_prob,
                'action': worst_action,
            },
            'expected_return': expected_return,
            'should_operate': expected_return > 0 and position_pct > 0,
        }

    # ─────────────────────────── 持仓管理 ───────────────────────────

    def _position_management(self, dragon: Dict,
                              buy_info: Dict) -> Dict[str, Any]:
        """生成持仓管理规则"""
        boards = dragon.get('limit_up_days', 1) or 1
        lifecycle = dragon['lifecycle_stage']
        seal_ratio = dragon.get('seal_ratio') or 0

        rules = []

        # 加仓条件
        add_rules = []
        if lifecycle == 'acceleration' and boards >= 2:
            add_rules.append("次日强势封板（封单比≥3%）→可加半仓")
            add_rules.append("3板确认龙头地位→可加至满仓计划")
        elif lifecycle == 'launch':
            add_rules.append("2板封板坚决→可加仓")
        rules.append("📈 加仓: " + ("；".join(add_rules) if add_rules else "暂不加仓，先确认"))

        # 减仓条件
        reduce_rules = []
        if boards >= 4:
            reduce_rules.append("断板日减半仓")
            reduce_rules.append("封单比降至1%以下→减仓")
        if lifecycle == 'climax':
            reduce_rules.append("爆量长上影→减仓")
            reduce_rules.append("板块跟风股批量炸板→减仓")
        reduce_rules.append("炸板率飙升至30%+→减仓")
        rules.append("📉 减仓: " + "；".join(reduce_rules))

        # 清仓条件
        clear_rules = []
        clear_rules.append("触发止损价→无条件清仓")
        if boards >= 3:
            clear_rules.append("断板且无法回封→清仓")
        clear_rules.append("龙头地位被替代（新龙头更高板）→清仓")
        if lifecycle == 'climax':
            clear_rules.append("放量跌停→次日开盘清仓")
        rules.append("🚫 清仓: " + "；".join(clear_rules))

        # 持有周期
        if lifecycle == 'launch':
            hold_period = "2-4天（看能否晋级确认）"
        elif lifecycle == 'acceleration':
            hold_period = "3-5天（持有至断板）"
        elif lifecycle == 'climax':
            hold_period = "1-2天（快进快出）"
        else:
            hold_period = "不持有"

        return {
            'rules': rules,
            'hold_period': hold_period,
        }

    # ─────────────────────────── 主流程 ───────────────────────────

    def generate_plans(self, date: str, top_n: int = 5,
                        save: bool = True,
                        capital_flow_multiplier: float = 1.0) -> List[Dict[str, Any]]:
        """
        为指定日期生成完整操作计划。

        capital_flow_multiplier: 资金流分析给出的仓位系数（0.1~1.5）。
        返回按优先级排序的操作计划列表。
        每个计划包含：龙头信息 + 买入策略 + 仓位 + 止损止盈 + 场景推演 + 持仓管理
        """
        logger.info(f"[{date}] 开始生成操作计划... 资金流系数={capital_flow_multiplier}")

        # 1. 识别龙头
        dragons = self.detector.detect_dragons(date, save=True)
        if not dragons:
            logger.warning(f"[{date}] 未识别到龙头，无操作计划")
            return []

        # 2. 获取市场环境
        smash = self._get_smash_coefficient(date)
        summary = self._get_daily_summary(date)
        market_env = self._assess_market_env(date, smash, summary)

        logger.info(f"[{date}] 市场环境: {market_env['name']}, "
                    f"砸盘系数{smash}, 炸板率{market_env['explosion_rate']:.1%}")

        # 3. 为每个龙头生成计划
        plans = []
        used_position = 0.0

        for dragon in dragons[:top_n]:
            # 跳过衰退期
            if dragon['lifecycle_stage'] == 'decline':
                continue

            # 买入策略
            buy_info = self._determine_buy_strategy(dragon, market_env)

            # 观望的不生成完整计划（但记录）
            if buy_info['strategy'] == 'wait':
                plans.append({
                    'dragon': dragon,
                    'action': 'wait',
                    'reason': buy_info['reason'],
                    'market_env': market_env,
                })
                continue

            # 仓位
            position_info = self._determine_position(
                dragon, market_env, used_position,
                capital_flow_multiplier=capital_flow_multiplier)

            # 仓位为0的跳过
            if position_info['position_pct'] <= 0:
                plans.append({
                    'dragon': dragon,
                    'action': 'skip',
                    'reason': f"市场环境{market_env['name']}，{dragon['certainty_level']}级标的不建议开仓",
                    'market_env': market_env,
                })
                continue

            used_position += position_info['position_pct']

            # 止损止盈
            exit_info = self._determine_stop_loss_take_profit(dragon, buy_info)

            # 场景推演
            scenario = self._scenario_analysis(dragon, buy_info, position_info)

            # 盈亏比检查（胜率优先：盈亏比<1.5不出计划）
            if exit_info['risk_reward_ratio'] < 1.5 and position_info['position_pct'] > 0.05:
                position_info['position_pct'] = round(position_info['position_pct'] * 0.5, 4)
                position_info['position_desc'] += "（盈亏比不足，仓位减半）"

            # 期望收益为负则不操作
            if not scenario['should_operate'] and scenario['expected_return'] < -1:
                plans.append({
                    'dragon': dragon,
                    'action': 'skip',
                    'reason': f"期望收益{scenario['expected_return']:.1f}%为负，不操作",
                    'market_env': market_env,
                })
                continue

            # 持仓管理
            mgmt = self._position_management(dragon, buy_info)

            plan = {
                'dragon': dragon,
                'action': 'operate',
                'market_env': market_env,
                'buy': buy_info,
                'position': position_info,
                'exit': exit_info,
                'scenario': scenario,
                'management': mgmt,
                'plan_date': date,
            }
            plans.append(plan)

        # 排序：操作计划优先，按期望收益排序
        operate_plans = [p for p in plans if p['action'] == 'operate']
        wait_plans = [p for p in plans if p['action'] != 'operate']
        operate_plans.sort(
            key=lambda p: p['scenario']['expected_return'], reverse=True)

        all_plans = operate_plans + wait_plans

        if save and operate_plans:
            self._save_plans(date, operate_plans)

        logger.info(f"[{date}] 操作计划生成完成: "
                    f"{len(operate_plans)}个可操作, {len(wait_plans)}个观望/跳过")

        return all_plans

    # ─────────────────────────── 数据持久化 ───────────────────────────

    @staticmethod
    def init_tables(db_path: str = DB_PATH):
        """创建操作计划表"""
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS operation_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_date TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT,
                dragon_type TEXT,
                certainty_level TEXT,
                total_score REAL,
                action TEXT,
                buy_strategy TEXT,
                buy_price_low REAL,
                buy_price_high REAL,
                position_pct REAL,
                stop_loss_price REAL,
                stop_loss_pct REAL,
                risk_reward_ratio REAL,
                expected_return REAL,
                best_case_prob REAL,
                best_case_return REAL,
                lifecycle_stage TEXT,
                plan_details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(plan_date, code)
            );
        """)
        conn.commit()
        conn.close()

    def _save_plans(self, date: str, plans: List[Dict]):
        """保存操作计划"""
        conn = self._get_conn()
        try:
            for p in plans:
                d = p['dragon']
                buy = p['buy']
                pos = p['position']
                exit_info = p['exit']
                scenario = p['scenario']

                conn.execute("""
                    INSERT OR REPLACE INTO operation_plans
                    (plan_date, code, name, dragon_type, certainty_level,
                     total_score, action, buy_strategy,
                     buy_price_low, buy_price_high, position_pct,
                     stop_loss_price, stop_loss_pct, risk_reward_ratio,
                     expected_return, best_case_prob, best_case_return,
                     lifecycle_stage, plan_details)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    date, d['code'], d['name'], d['dragon_type'],
                    d['certainty_level'], d['total_score'],
                    p['action'], buy['strategy'],
                    buy.get('price_range', {}).get('buy_price_low', 0),
                    buy.get('price_range', {}).get('buy_price_high', 0),
                    pos['position_pct'],
                    exit_info['stop_loss_price'],
                    exit_info['stop_loss_pct'],
                    exit_info['risk_reward_ratio'],
                    scenario['expected_return'],
                    scenario['best_case']['probability'],
                    scenario['best_case']['return_pct'],
                    d['lifecycle_stage'],
                    json.dumps({
                        'buy_conditions': buy.get('conditions', []),
                        'stop_rules': exit_info.get('stop_rules', []),
                        'take_profit_rules': exit_info.get('take_profit_rules', []),
                        'management_rules': p['management'].get('rules', []),
                        'scenarios': {
                            'best': scenario['best_case'],
                            'neutral': scenario['neutral_case'],
                            'worst': scenario['worst_case'],
                        },
                    }, ensure_ascii=False),
                ))
            conn.commit()
        finally:
            conn.close()

    # ─────────────────────────── 输出格式化 ───────────────────────────

    def format_plans(self, plans: List[Dict], date: str = '') -> str:
        """格式化操作计划报告"""
        lines = []
        lines.append(f"{'='*65}")
        lines.append(f"  📋 确定性龙头操作计划 | {date}")
        lines.append(f"{'='*65}")
        lines.append("")

        if not plans:
            lines.append("  ❌ 当日无操作计划")
            lines.append("  建议：市场无确定性龙头，空仓观望")
            return '\n'.join(lines)

        # 市场环境
        env = plans[0].get('market_env', {})
        lines.append(f"  🌡️ 市场环境: {env.get('name', '未知')}")
        lines.append(f"     砸盘系数: {env.get('smash', 'N/A')} | "
                     f"炸板率: {env.get('explosion_rate', 0):.1%} | "
                     f"涨停数: {env.get('limit_up_count', 0)}")
        lines.append("")

        operate_plans = [p for p in plans if p['action'] == 'operate']
        wait_plans = [p for p in plans if p['action'] != 'operate']

        if operate_plans:
            lines.append(f"  🎯 可操作标的（{len(operate_plans)}只）:")
            lines.append(f"{'-'*65}")

            for i, p in enumerate(operate_plans, 1):
                d = p['dragon']
                buy = p['buy']
                pos = p['position']
                exit_info = p['exit']
                scenario = p['scenario']
                mgmt = p['management']

                type_info = DRAGON_TYPES[d['dragon_type']]
                level_icon = {'SS': '🔴', 'S': '🟠', 'A': '🟡', 'B': '⚪'}
                icon = level_icon.get(d['certainty_level'], '⚪')

                lines.append("")
                lines.append(f"  {i}. {icon} 【{d['certainty_level']}级】{d['name']}({d['code']})")
                lines.append(f"     {type_info['color']}{d['dragon_type_name']} | "
                            f"{d['lifecycle_name']} | "
                            f"评分{d['total_score']} | "
                            f"{d['limit_up_days']}板")

                # 买入策略
                lines.append(f"     ┌─ 买入策略 ─────────────────────────")
                lines.append(f"     │ {buy.get('strategy_icon', '')} {buy['strategy_name']}: {buy['reason']}")
                if buy.get('price_range'):
                    lines.append(f"     │ 💰 价位: {buy['price_range'].get('buy_price_desc', 'N/A')}")
                lines.append(f"     │ ⏰ 时机: {buy['timing']}")
                for cond in buy.get('conditions', [])[:3]:
                    lines.append(f"     │ • {cond}")

                # 仓位
                lines.append(f"     ├─ 仓位决策 ─────────────────────────")
                lines.append(f"     │ 📊 {pos['position_desc']}")
                if pos.get('batch_plan'):
                    for bp in pos['batch_plan']:
                        lines.append(f"     │ • {bp}")

                # 止损止盈
                lines.append(f"     ├─ 止损止盈 ─────────────────────────")
                lines.append(f"     │ 🛑 止损价: {exit_info['stop_loss_price']:.2f} "
                            f"({exit_info['stop_loss_pct']:.0f}%)")
                for sr in exit_info.get('stop_rules', [])[:2]:
                    lines.append(f"     │ • {sr}")
                for tp in exit_info.get('take_profit_targets', [])[:2]:
                    lines.append(f"     │ ✅ {tp['desc']} → {tp['target']:.2f}（{tp['action']}）")
                lines.append(f"     │ ⚖️ 盈亏比: 1:{exit_info['risk_reward_ratio']:.1f}")

                # 场景推演
                lines.append(f"     ├─ 场景推演 ─────────────────────────")
                lines.append(f"     │ 🟢 最好({scenario['best_case']['probability']:.0%}): "
                            f"{scenario['best_case']['scenario']} "
                            f"+{scenario['best_case']['return_pct']:.0f}%")
                lines.append(f"     │ 🟡 基准({scenario['neutral_case']['probability']:.0%}): "
                            f"{scenario['neutral_case']['scenario']} "
                            f"+{scenario['neutral_case']['return_pct']:.0f}%")
                lines.append(f"     │ 🔴 最坏({scenario['worst_case']['probability']:.0%}): "
                            f"{scenario['worst_case']['scenario']} "
                            f"{scenario['worst_case']['return_pct']:.0f}%")
                lines.append(f"     │ 📈 期望收益: {scenario['expected_return']:+.1f}%")

                # 持仓管理
                lines.append(f"     └─ 持仓管理 ─────────────────────────")
                lines.append(f"       持有周期: {mgmt['hold_period']}")
                for rule in mgmt.get('rules', []):
                    lines.append(f"       {rule}")

        if wait_plans:
            lines.append("")
            lines.append(f"  ⏸️ 观望/不操作（{len(wait_plans)}只）:")
            for p in wait_plans[:5]:
                d = p['dragon']
                lines.append(f"     • {d['name']}({d['code']}) - {p.get('reason', '条件不满足')}")

        lines.append("")
        lines.append(f"{'='*65}")
        lines.append("⚠️ 声明：以上为AI模型分析结果，仅供参考，不构成投资建议。")
        lines.append(f"{'='*65}")
        return '\n'.join(lines)

    def get_brief_summary(self, plans: List[Dict]) -> str:
        """获取简短摘要（用于推送/通知）"""
        operate = [p for p in plans if p['action'] == 'operate']
        if not operate:
            return "今日无确定性操作机会，建议观望。"

        lines = []
        for p in operate[:3]:
            d = p['dragon']
            buy = p['buy']
            pos = p['position']
            lines.append(
                f"【{d['certainty_level']}级】{d['name']}({d['code']}) "
                f"{d['limit_up_days']}板{d['dragon_type_name']} | "
                f"{buy['strategy_name']} {pos['position_desc']} | "
                f"止损{p['exit']['stop_loss_price']:.2f} | "
                f"期望收益{p['scenario']['expected_return']:+.1f}%"
            )
        return '\n'.join(lines)


# ─────────────────────────── 便捷函数 ───────────────────────────

def generate_plans(date: str = None, db_path: str = DB_PATH,
                    top_n: int = 5) -> List[Dict]:
    """便捷函数：生成操作计划"""
    DragonDetector.init_tables(db_path)
    OperationPlanner.init_tables(db_path)
    planner = OperationPlanner(db_path)
    if date is None:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT MAX(date) as d FROM xgt_daily_summary").fetchone()
        date = row['d'] if row else None
        conn.close()
    if not date:
        return []
    return planner.generate_plans(date, top_n=top_n)


# ─────────────────────────── 主程序 ───────────────────────────

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='确定性龙头操作计划')
    parser.add_argument('--date', type=str, default=None, help='指定日期')
    parser.add_argument('--top', type=int, default=5, help='计划数量')
    parser.add_argument('--db', type=str, default=DB_PATH, help='数据库路径')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                       format='%(asctime)s [%(levelname)s] %(message)s',
                       datefmt='%Y-%m-%d %H:%M:%S')

    DragonDetector.init_tables(args.db)
    OperationPlanner.init_tables(args.db)
    planner = OperationPlanner(args.db)

    if args.date:
        date = args.date
    else:
        conn = sqlite3.connect(args.db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT MAX(date) as d FROM xgt_daily_summary").fetchone()
        date = row['d'] if row else None
        conn.close()

    if not date:
        print("无法获取最新交易日")
        exit(1)

    print(f"分析日期: {date}\n")
    plans = planner.generate_plans(date, top_n=args.top)
    report = planner.format_plans(plans, date)
    print(report)
