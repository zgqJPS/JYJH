"""
capital_flow_analyzer.py - 资金进攻、持续、轮动分析引擎

三大分析维度：
1. 进攻力度（Attack）：资金做多意愿和攻击强度
   - 涨停家数趋势、封板率、炸板率反向
   - 首板占比 vs 连板占比（资金是广撒网还是聚焦）
   - 一字板/秒板占比（抢筹强度）
   - 涨停时间分布（早盘涨停=进攻强）
   - 涨跌家数比（市场广度）

2. 持续能力（Persistence）：资金接力意愿和趋势延续
   - 连板晋级率（2板→3板、3板→4板等）
   - 连板高度趋势（最高板变化）
   - 板块连续涨停天数
   - 昨日涨停今日平均溢价
   - 高标存活率（≥3板次日不跌停的比例）
   - 封单稳定性（炸板回封率）

3. 轮动习惯（Rotation）：资金在板块间的流动模式
   - 板块持续性（连续N天有涨停的板块）
   - 板块切换速度（新板块出现频率）
   - 主线集中度（Top3板块涨停数占比）
   - 资金流向（老板块退潮→新板块崛起）
   - 轮动周期（从启动到退潮的天数）

设计原则：
- 胜率第一，宁可保守不误判
- 所有评分0-100，越高越积极
- 输出结构化结论，可直接用于操作计划调整
"""
import logging
import sqlite3
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any

from config import DB_PATH

try:
    from board_calculator import BoardCalculator
    _HAS_BOARD_CALC = True
except ImportError:
    _HAS_BOARD_CALC = False

logger = logging.getLogger(__name__)


# ============================================================
# 常量定义
# ============================================================

# 市场进攻等级
ATTACK_LEVELS = {
    'strong': {'name': '强攻', 'emoji': '🔴', 'min_score': 70,
               'desc': '资金猛烈进攻，做多意愿极强'},
    'moderate': {'name': '温和进攻', 'emoji': '🟠', 'min_score': 50,
                 'desc': '资金有序进攻，可精选参与'},
    'weak': {'name': '弱攻', 'emoji': '🟡', 'min_score': 30,
             'desc': '资金进攻乏力，谨慎参与'},
    'defensive': {'name': '防守', 'emoji': '🟢', 'min_score': 0,
                  'desc': '资金无意进攻，观望为主'},
}

# 持续性等级
PERSISTENCE_LEVELS = {
    'strong': {'name': '强持续', 'emoji': '🔴', 'min_score': 70,
               'desc': '接力意愿强，赚钱效应持续'},
    'moderate': {'name': '中等持续', 'emoji': '🟠', 'min_score': 50,
                 'desc': '有一定接力，精选龙头可参与'},
    'weak': {'name': '弱持续', 'emoji': '🟡', 'min_score': 30,
             'desc': '接力意愿差，快进快出'},
    'none': {'name': '无持续', 'emoji': '🟢', 'min_score': 0,
             'desc': '一日游行情，不参与'},
}

# 轮动模式
ROTATION_PATTERNS = {
    'mainline': {'name': '主线主导', 'emoji': '🎯',
                 'desc': '资金聚焦主线板块，持续性好，重仓主线龙头'},
    'rotation': {'name': '板块轮动', 'emoji': '🔄',
                 'desc': '资金在板块间快速切换，低吸新启动板块'},
    'diffusion': {'name': '全面扩散', 'emoji': '🌊',
                  'desc': '资金全面开花，普涨行情，持有待涨'},
    'contraction': {'name': '收缩防守', 'emoji': '🛡️',
                    'desc': '资金收缩，仅少数板块活跃，减仓观望'},
    'chaos': {'name': '无序轮动', 'emoji': '🌀',
              'desc': '板块无规律切换，资金没有方向，空仓等待'},
}

# 早盘涨停截止时间（秒）
EARLY_LIMIT_UP_DEADLINE = 10 * 3600 + 30 * 60  # 10:30:00
MORNING_LIMIT_UP_DEADLINE = 11 * 3600 + 30 * 60  # 11:30:00


def _time_to_seconds(time_str: str) -> int:
    """将 HH:MM:SS 转换为秒数"""
    if not time_str or not isinstance(time_str, str):
        return 99999
    try:
        parts = time_str.strip().split(':')
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        elif len(parts) == 2:
            return int(parts[0]) * 3600 + int(parts[1]) * 60
    except (ValueError, IndexError):
        pass
    return 99999


def _safe_float(val, default=0.0):
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default


def _safe_int(val, default=0):
    try:
        return int(val) if val is not None else default
    except (ValueError, TypeError):
        return default


# ============================================================
# 资金流分析引擎
# ============================================================

class CapitalFlowAnalyzer:
    """资金进攻、持续、轮动分析引擎"""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._board_calc = None
        if _HAS_BOARD_CALC:
            try:
                self._board_calc = BoardCalculator(self.conn)
            except Exception as e:
                logger.warning(f"BoardCalculator初始化失败: {e}")

    def _apply_real_boards(self, stocks: List[Dict], date: str) -> List[Dict]:
        """用BoardCalculator真实连板数覆盖API的limit_up_days字段"""
        if not self._board_calc or not stocks:
            return stocks
        try:
            for s in stocks:
                real = self._board_calc.get_consecutive_boards(date, s['code'], self.conn)
                if real > 0:
                    s['api_limit_up_days'] = s.get('limit_up_days', 1)
                    s['limit_up_days'] = real
        except Exception as e:
            logger.warning(f"真实连板数覆盖失败({date}): {e}")
        return stocks

    def close(self):
        if self.conn:
            self.conn.close()

    def _get_trading_dates(self, end_date: str, count: int = 20) -> List[str]:
        """获取截止end_date的最近count个交易日"""
        rows = self.conn.execute(
            "SELECT DISTINCT date FROM xgt_limit_up_detail WHERE date <= ? "
            "ORDER BY date DESC LIMIT ?",
            (end_date, count)
        ).fetchall()
        return [r[0] for r in reversed(rows)]

    def _get_limit_up_stocks(self, date: str) -> List[Dict]:
        rows = self.conn.execute(
            "SELECT * FROM xgt_limit_up_detail WHERE date = ?", (date,)
        ).fetchall()
        result = []
        for r in rows:
            stock = dict(r)
            # 换手率归一化：>1视为百分数（如5=5%），>100或负数视为异常
            tr = stock.get('turnover_rate')
            if tr is not None:
                try:
                    tr = float(tr)
                    if tr > 100 or tr < 0:
                        stock['turnover_rate'] = 0.15
                    elif tr > 1.0:
                        stock['turnover_rate'] = tr / 100.0
                except (TypeError, ValueError):
                    stock['turnover_rate'] = 0.15
            # 首封时间归一化："092500" → "09:25:00"
            ft = stock.get('first_limit_up_time')
            if ft and ':' not in str(ft):
                ft_str = str(ft).strip()
                if len(ft_str) >= 6:
                    stock['first_limit_up_time'] = f"{ft_str[:2]}:{ft_str[2:4]}:{ft_str[4:6]}"
                elif len(ft_str) == 4:
                    stock['first_limit_up_time'] = f"{ft_str[:2]}:{ft_str[2:]}:00"
            result.append(stock)
        # 用真实连板数覆盖API字段
        self._apply_real_boards(result, date)
        return result

    def _get_daily_summary(self, date: str) -> Optional[Dict]:
        row = self.conn.execute(
            "SELECT * FROM xgt_daily_summary WHERE date = ?", (date,)
        ).fetchone()
        return dict(row) if row else None

    def _get_concept_stats(self, date: str) -> Dict[str, int]:
        """获取非炸板的概念统计"""
        rows = self.conn.execute(
            "SELECT concept, count FROM concept_statistics "
            "WHERE date = ? AND concept NOT LIKE '%炸板%'",
            (date,)
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def _get_break_stats(self, date: str) -> Dict[str, int]:
        """获取炸板概念统计"""
        rows = self.conn.execute(
            "SELECT concept, count FROM concept_statistics "
            "WHERE date = ? AND concept LIKE '%炸板%'",
            (date,)
        ).fetchall()
        result = {}
        for r in rows:
            # 去掉"(炸板)"后缀
            name = r[0].replace('(炸板)', '')
            result[name] = r[1]
        return result

    def _get_prev_trading_day(self, date: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT MAX(date) FROM xgt_limit_up_detail WHERE date < ?", (date,)
        ).fetchone()
        return row[0] if row and row[0] else None

    # ============================================================
    # 维度一：进攻力度
    # ============================================================

    def analyze_attack(self, date: str, history_days: int = 5) -> Dict:
        """
        分析资金进攻力度。

        指标组成：
        - 涨停家数趋势（30%）：当日涨停数 vs 近5日均值
        - 封板质量（25%）：封板率=涨停/(涨停+炸板)
        - 早盘涨停占比（20%）：10:30前涨停占比
        - 市场广度（15%）：涨跌家数比
        - 连板结构（10%）：连板股占比（资金聚焦度）
        """
        stocks = self._get_limit_up_stocks(date)
        summary = self._get_daily_summary(date)
        dates = self._get_trading_dates(date, history_days + 1)

        if not stocks:
            return self._empty_attack_result(date)

        total_limit_up = len(stocks)
        break_count = summary.get('break_limit_up_count', 0) if summary else 0

        # 1. 涨停家数趋势（30分）
        recent_counts = []
        for d in dates[:-1]:  # 不含当日
            cnt = self.conn.execute(
                "SELECT COUNT(*) FROM xgt_limit_up_detail WHERE date = ?", (d,)
            ).fetchone()[0]
            if cnt > 0:
                recent_counts.append(cnt)
        avg_count = sum(recent_counts) / len(recent_counts) if recent_counts else total_limit_up
        count_ratio = total_limit_up / avg_count if avg_count > 0 else 1.0
        # 比值1.0→15分（中性），1.3+→30分（强），0.7以下→5分（弱）
        if count_ratio >= 1.3:
            count_score = 30
        elif count_ratio >= 1.1:
            count_score = 25
        elif count_ratio >= 0.9:
            count_score = 18
        elif count_ratio >= 0.7:
            count_score = 10
        else:
            count_score = 5

        # 2. 封板质量（25分）
        total_attempts = total_limit_up + break_count
        seal_rate = total_limit_up / total_attempts if total_attempts > 0 else 0
        # 封板率>85%→25分，70-85%→20分，55-70%→12分，<55%→5分
        if seal_rate >= 0.85:
            seal_score = 25
        elif seal_rate >= 0.70:
            seal_score = 20
        elif seal_rate >= 0.55:
            seal_score = 12
        else:
            seal_score = 5

        # 3. 早盘涨停占比（20分）
        early_count = 0
        morning_count = 0
        for s in stocks:
            t = _time_to_seconds(s.get('first_limit_up_time', ''))
            if t <= EARLY_LIMIT_UP_DEADLINE:
                early_count += 1
            if t <= MORNING_LIMIT_UP_DEADLINE:
                morning_count += 1
        early_ratio = early_count / total_limit_up if total_limit_up > 0 else 0
        # 早盘涨停>50%→20分，30-50%→15分，15-30%→10分，<15%→5分
        if early_ratio >= 0.50:
            early_score = 20
        elif early_ratio >= 0.30:
            early_score = 15
        elif early_ratio >= 0.15:
            early_score = 10
        else:
            early_score = 5

        # 4. 市场广度（15分）
        rise_count = _safe_int(summary.get('rise_count', 0)) if summary else 0
        fall_count = _safe_int(summary.get('fall_count', 0)) if summary else 0
        if rise_count + fall_count > 0:
            breadth = rise_count / (rise_count + fall_count)
        else:
            breadth = 0.5
        # 涨家数占比>65%→15分，50-65%→12分，35-50%→7分，<35%→3分
        if breadth >= 0.65:
            breadth_score = 15
        elif breadth >= 0.50:
            breadth_score = 12
        elif breadth >= 0.35:
            breadth_score = 7
        else:
            breadth_score = 3

        # 5. 连板结构（10分）
        lianban_count = sum(1 for s in stocks
                           if _safe_int(s.get('limit_up_days')) >= 2)
        lianban_ratio = lianban_count / total_limit_up if total_limit_up > 0 else 0
        # 连板占比>25%→10分（资金聚焦连板），15-25%→8分，<15%→5分
        if lianban_ratio >= 0.25:
            structure_score = 10
        elif lianban_ratio >= 0.15:
            structure_score = 8
        else:
            structure_score = 5

        total_score = count_score + seal_score + early_score + breadth_score + structure_score

        # 进攻等级
        level = 'defensive'
        for key in ['strong', 'moderate', 'weak']:
            if total_score >= ATTACK_LEVELS[key]['min_score']:
                level = key
                break

        # 一字板/秒板统计（抢筹强度）
        yizi_count = sum(1 for s in stocks
                        if s.get('first_limit_up_time') in ('09:25:00', '09:25', '09:30:00', '09:30')
                        or (_safe_float(s.get('seal_ratio')) >= 0.15
                            and _time_to_seconds(s.get('first_limit_up_time', '')) <= 9 * 3600 + 35 * 60))

        return {
            'date': date,
            'score': total_score,
            'level': level,
            'level_name': ATTACK_LEVELS[level]['name'],
            'level_emoji': ATTACK_LEVELS[level]['emoji'],
            'level_desc': ATTACK_LEVELS[level]['desc'],
            'metrics': {
                'total_limit_up': total_limit_up,
                'break_count': break_count,
                'seal_rate': round(seal_rate, 3),
                'early_count': early_count,
                'early_ratio': round(early_ratio, 3),
                'morning_ratio': round(morning_count / total_limit_up, 3) if total_limit_up else 0,
                'avg_recent_count': round(avg_count, 1),
                'count_ratio': round(count_ratio, 2),
                'rise_count': rise_count,
                'fall_count': fall_count,
                'breadth': round(breadth, 3),
                'lianban_count': lianban_count,
                'lianban_ratio': round(lianban_ratio, 3),
                'yizi_count': yizi_count,
            },
            'sub_scores': {
                'count_trend': count_score,
                'seal_quality': seal_score,
                'early_attack': early_score,
                'breadth': breadth_score,
                'structure': structure_score,
            },
            'signals': self._generate_attack_signals(
                total_score, count_ratio, seal_rate, early_ratio,
                breadth, lianban_ratio, yizi_count, total_limit_up),
        }

    def _empty_attack_result(self, date: str) -> Dict:
        return {
            'date': date, 'score': 0, 'level': 'defensive',
            'level_name': '防守', 'level_emoji': '🟢',
            'level_desc': '无涨停数据', 'metrics': {},
            'sub_scores': {}, 'signals': ['无涨停数据，市场极度弱势'],
        }

    def _generate_attack_signals(self, score, count_ratio, seal_rate,
                                  early_ratio, breadth, lianban_ratio,
                                  yizi_count, total_lu) -> List[str]:
        signals = []
        if count_ratio >= 1.3:
            signals.append(f"涨停家数放量至近5日均量的{count_ratio:.1%}，资金大幅入场")
        elif count_ratio >= 1.1:
            signals.append(f"涨停家数温和放大({count_ratio:.1%})，资金缓慢进场")
        elif count_ratio <= 0.7:
            signals.append(f"涨停家数骤降至均量的{count_ratio:.1%}，资金大幅撤退")

        if seal_rate >= 0.85:
            signals.append(f"封板率{seal_rate:.0%}，封板质量极高")
        elif seal_rate >= 0.70:
            signals.append(f"封板率{seal_rate:.0%}，封板质量良好")
        elif seal_rate < 0.55:
            signals.append(f"封板率仅{seal_rate:.0%}，封板质量差，炸板风险高")

        if early_ratio >= 0.50:
            signals.append(f"早盘涨停占比{early_ratio:.0%}，抢筹意愿极强")
        elif early_ratio < 0.15:
            signals.append(f"早盘涨停占比仅{early_ratio:.0%}，资金攻击犹豫")

        if yizi_count >= 5:
            signals.append(f"一字/秒板{yizi_count}只，抢筹强度极高")

        if breadth < 0.35:
            signals.append(f"涨家数仅占{breadth:.0%}，市场普跌，涨停多为独立行情")

        if lianban_ratio >= 0.25:
            signals.append(f"连板股占比{lianban_ratio:.0%}，资金聚焦连板龙头")
        elif lianban_ratio < 0.10 and total_lu > 30:
            signals.append(f"连板股占比仅{lianban_ratio:.0%}，资金广撒网首板")

        return signals

    # ============================================================
    # 维度二：持续能力
    # ============================================================

    def analyze_persistence(self, date: str, history_days: int = 10) -> Dict:
        """
        分析资金持续/接力能力。

        指标组成：
        - 连板晋级率（30%）：各板级晋升比率综合
        - 连板高度趋势（20%）：近5日最高板变化
        - 板块持续性（20%）：连续涨停板块的天数
        - 高标存活率（15%）：前日≥3板今日表现
        - 封单回封率（15%）：炸板后回封比例
        """
        stocks = self._get_limit_up_stocks(date)
        summary = self._get_daily_summary(date)
        prev_date = self._get_prev_trading_day(date)
        dates = self._get_trading_dates(date, history_days + 1)

        if not stocks:
            return self._empty_persistence_result(date)

        # 1. 连板晋级率（30分）
        # 计算方式：今日N板股票数 / 昨日(N-1)板股票数
        promotion_rates = []
        if prev_date:
            prev_stocks = self._get_limit_up_stocks(prev_date)
            prev_board_dist = Counter(
                _safe_int(s.get('limit_up_days')) for s in prev_stocks
                if _safe_int(s.get('limit_up_days')) >= 1
            )
            today_board_dist = Counter(
                _safe_int(s.get('limit_up_days')) for s in stocks
                if _safe_int(s.get('limit_up_days')) >= 2
            )

            for board_level in range(2, 8):
                prev_count = prev_board_dist.get(board_level - 1, 0)
                today_count = today_board_dist.get(board_level, 0)
                if prev_count > 0:
                    rate = today_count / prev_count
                    promotion_rates.append(min(rate, 1.0))  # 截断到1.0

        avg_promotion = (sum(promotion_rates) / len(promotion_rates)
                        if promotion_rates else 0.3)
        # 晋级率>60%→30分，40-60%→22分，25-40%→15分，<25%→8分
        if avg_promotion >= 0.60:
            promo_score = 30
        elif avg_promotion >= 0.40:
            promo_score = 22
        elif avg_promotion >= 0.25:
            promo_score = 15
        else:
            promo_score = 8

        # 2. 连板高度趋势（20分）—— 优先使用BoardCalculator真实最高板
        max_boards_series = []
        for d in dates:
            mb = 0
            if self._board_calc:
                try:
                    mb = self._board_calc.get_daily_max_boards(d, self.conn)
                except Exception:
                    mb = 0
            if mb == 0:
                row = self.conn.execute(
                    "SELECT MAX(limit_up_days) FROM xgt_limit_up_detail WHERE date = ?",
                    (d,)
                ).fetchone()
                mb = _safe_int(row[0]) if row and row[0] else 0
            if mb > 0:
                max_boards_series.append(mb)

        today_max = max_boards_series[-1] if max_boards_series else 0
        prev_max = max_boards_series[-2] if len(max_boards_series) >= 2 else 0
        # 高度上升/维持高位→高分
        if today_max >= 7:
            height_score = 20
        elif today_max >= 5:
            height_score = 16
        elif today_max >= 3:
            height_score = 12
        else:
            height_score = 6
        # 高度较前日下降超过2板额外扣分
        if prev_max > 0 and today_max < prev_max - 2:
            height_score = max(4, height_score - 5)

        # 3. 板块持续性（20分）
        # 统计近5天连续出现涨停的板块
        concept_stats_history = {}
        for d in dates[-5:]:  # 近5天
            concept_stats_history[d] = self._get_concept_stats(d)

        # 找出连续N天出现的板块
        all_concepts = set()
        for d_stats in concept_stats_history.values():
            all_concepts.update(d_stats.keys())

        concept_persistence = {}
        for concept in all_concepts:
            consecutive = 0
            for d in reversed(dates[-5:]):
                if concept in concept_stats_history.get(d, {}):
                    consecutive += 1
                else:
                    break
            if consecutive >= 2:
                concept_persistence[concept] = consecutive

        persistent_concepts = len(concept_persistence)
        max_consecutive = max(concept_persistence.values()) if concept_persistence else 0
        # 有≥3个板块持续3天以上→20分，有持续板块→12-16分，无→5分
        strong_persistent = sum(1 for v in concept_persistence.values() if v >= 3)
        if strong_persistent >= 2 and max_consecutive >= 4:
            concept_score = 20
        elif strong_persistent >= 1:
            concept_score = 16
        elif persistent_concepts >= 3:
            concept_score = 12
        elif persistent_concepts >= 1:
            concept_score = 8
        else:
            concept_score = 5

        # 4. 高标存活率（15分）
        high_survival_rate = 0.5  # 默认
        high_board_codes = []
        if prev_date:
            prev_high = [s for s in (self._get_limit_up_stocks(prev_date))
                        if _safe_int(s.get('limit_up_days')) >= 3]
            high_board_codes = [s['code'] for s in prev_high]
            if prev_high:
                # 今日仍涨停的
                today_codes = {s['code'] for s in stocks}
                survived = sum(1 for c in high_board_codes if c in today_codes)
                high_survival_rate = survived / len(prev_high)

        # 存活率>60%→15分，40-60%→11分，<40%→6分
        if high_survival_rate >= 0.60:
            survival_score = 15
        elif high_survival_rate >= 0.40:
            survival_score = 11
        else:
            survival_score = 6

        # 5. 炸板回封率（15分）
        # break_times=0表示未炸板或炸板后回封，用有break_times记录的来估算
        total_with_break = 0
        recovered = 0
        for s in stocks:
            bt = _safe_int(s.get('break_times'))
            if bt > 0:
                total_with_break += 1
                recovered += 1  # 在涨停列表中且有炸板记录=回封成功
        break_count = summary.get('break_limit_up_count', 0) if summary else 0
        total_break_attempts = total_with_break + break_count
        recovery_rate = recovered / total_break_attempts if total_break_attempts > 0 else 0.8
        # 回封率>70%→15分，50-70%→11分，<50%→6分
        if recovery_rate >= 0.70:
            recovery_score = 15
        elif recovery_rate >= 0.50:
            recovery_score = 11
        else:
            recovery_score = 6

        total_score = promo_score + height_score + concept_score + survival_score + recovery_score

        level = 'none'
        for key in ['strong', 'moderate', 'weak']:
            if total_score >= PERSISTENCE_LEVELS[key]['min_score']:
                level = key
                break

        return {
            'date': date,
            'score': total_score,
            'level': level,
            'level_name': PERSISTENCE_LEVELS[level]['name'],
            'level_emoji': PERSISTENCE_LEVELS[level]['emoji'],
            'level_desc': PERSISTENCE_LEVELS[level]['desc'],
            'metrics': {
                'avg_promotion_rate': round(avg_promotion, 3),
                'promotion_details': [round(r, 3) for r in promotion_rates],
                'today_max_boards': today_max,
                'prev_max_boards': prev_max,
                'max_boards_trend': max_boards_series[-5:],
                'persistent_concepts': concept_persistence,
                'persistent_concept_count': persistent_concepts,
                'max_consecutive_days': max_consecutive,
                'high_board_count_prev': len(high_board_codes),
                'high_survival_rate': round(high_survival_rate, 3),
                'break_recovery_rate': round(recovery_rate, 3),
            },
            'sub_scores': {
                'promotion': promo_score,
                'height_trend': height_score,
                'concept_persistence': concept_score,
                'high_survival': survival_score,
                'recovery': recovery_score,
            },
            'signals': self._generate_persistence_signals(
                total_score, avg_promotion, today_max, prev_max,
                concept_persistence, high_survival_rate, recovery_rate),
        }

    def _empty_persistence_result(self, date: str) -> Dict:
        return {
            'date': date, 'score': 0, 'level': 'none',
            'level_name': '无持续', 'level_emoji': '🟢',
            'level_desc': '无数据', 'metrics': {},
            'sub_scores': {}, 'signals': ['无涨停数据，无法评估持续性'],
        }

    def _generate_persistence_signals(self, score, promo_rate, today_max,
                                       prev_max, persistent_concepts,
                                       survival_rate, recovery_rate) -> List[str]:
        signals = []
        if promo_rate >= 0.60:
            signals.append(f"平均晋级率{promo_rate:.0%}，接力情绪高涨")
        elif promo_rate >= 0.40:
            signals.append(f"平均晋级率{promo_rate:.0%}，接力尚可")
        elif promo_rate < 0.25:
            signals.append(f"平均晋级率仅{promo_rate:.0%}，接力断层严重")

        if today_max > prev_max:
            signals.append(f"最高板从{prev_max}板升至{today_max}板，空间打开")
        elif today_max < prev_max - 2:
            signals.append(f"最高板从{prev_max}板降至{today_max}板，空间压缩")

        if persistent_concepts:
            top = sorted(persistent_concepts.items(), key=lambda x: -x[1])[:3]
            desc = ', '.join([f"{c}({d}天)" for c, d in top])
            signals.append(f"持续板块: {desc}")

        if survival_rate < 0.40:
            signals.append(f"高标存活率仅{survival_rate:.0%}，高标风险大")

        if recovery_rate < 0.50:
            signals.append(f"炸板回封率{recovery_rate:.0%}，封单不稳")

        return signals

    # ============================================================
    # 维度三：轮动习惯
    # ============================================================

    def analyze_rotation(self, date: str, history_days: int = 10) -> Dict:
        """
        分析资金轮动习惯。

        指标组成：
        - 主线集中度（30%）：Top3板块涨停数占比
        - 板块持续性（25%）：连续活跃板块数量
        - 新旧切换（20%）：今日新出现板块 vs 昨日板块消退
        - 轮动速度（15%）：近5日主线板块变化频率
        - 资金流向（10%）：从老板块到新板块的流动强度
        """
        stocks = self._get_limit_up_stocks(date)
        dates = self._get_trading_dates(date, history_days + 1)
        prev_date = self._get_prev_trading_day(date)

        if not stocks:
            return self._empty_rotation_result(date)

        today_concepts = self._get_concept_stats(date)
        break_concepts = self._get_break_stats(date)

        # 过滤掉"其他"
        if '其他' in today_concepts:
            del today_concepts['其他']

        total_concept_stocks = sum(today_concepts.values())
        if total_concept_stocks == 0:
            return self._empty_rotation_result(date)

        # 1. 主线集中度（30分）
        sorted_concepts = sorted(today_concepts.items(), key=lambda x: -x[1])
        top3_count = sum(c[1] for c in sorted_concepts[:3])
        top3_ratio = top3_count / total_concept_stocks if total_concept_stocks > 0 else 0

        # 集中度>60%→25分（主线清晰），45-60%→20分，30-45%→12分，<30%→5分
        if top3_ratio >= 0.60:
            concentration_score = 28
        elif top3_ratio >= 0.45:
            concentration_score = 22
        elif top3_ratio >= 0.30:
            concentration_score = 14
        else:
            concentration_score = 6

        # 2. 板块持续性（25分）- 与persistence有重叠但视角不同
        # 这里关注"主线板块"的持续天数
        concept_days = {}  # concept -> 连续天数
        if prev_date:
            # 回溯计算每个top概念的连续天数
            for concept_name in today_concepts:
                consecutive = 0
                for d in reversed(dates):
                    stats = self._get_concept_stats(d)
                    if concept_name in stats and stats[concept_name] >= 1:
                        consecutive += 1
                    else:
                        break
                if consecutive >= 2:
                    concept_days[concept_name] = consecutive

        top_concept = sorted_concepts[0][0] if sorted_concepts else ''
        top_concept_days = concept_days.get(top_concept, 1)
        # 主线持续≥4天→25分，2-3天→18分，1天→10分
        if top_concept_days >= 4:
            persist_score = 25
        elif top_concept_days >= 2:
            persist_score = 18
        else:
            persist_score = 10

        # 3. 新旧切换（20分）
        prev_concepts = {}
        if prev_date:
            prev_concepts = self._get_concept_stats(prev_date)
            if '其他' in prev_concepts:
                del prev_concepts['其他']

        new_concepts = set(today_concepts.keys()) - set(prev_concepts.keys()) if prev_concepts else set()
        faded_concepts = set(prev_concepts.keys()) - set(today_concepts.keys()) if prev_concepts else set()

        new_count = sum(today_concepts.get(c, 0) for c in new_concepts)
        new_ratio = new_count / total_concept_stocks if total_concept_stocks > 0 else 0

        # 新板块占比适度（10-30%）→高分（有新血液但不混乱）
        # 过高（>50%）→板块乱切换，过低（<5%）→无新题材
        if 0.10 <= new_ratio <= 0.30:
            switch_score = 20
        elif 0.05 <= new_ratio < 0.10 or 0.30 < new_ratio <= 0.45:
            switch_score = 15
        elif new_ratio > 0.45:
            switch_score = 8  # 太多新板块=无序
        else:
            switch_score = 10  # 没有新板块=缺乏活力

        # 4. 轮动速度（15分）
        # 近5日每天Top1板块的变化次数
        top1_history = []
        for d in dates[-5:]:
            stats = self._get_concept_stats(d)
            if '其他' in stats:
                del stats['其他']
            if stats:
                top1 = max(stats, key=stats.get)
                top1_history.append(top1)

        changes = sum(1 for i in range(1, len(top1_history))
                     if top1_history[i] != top1_history[i-1])
        # 0-1次变化→15分（主线稳定），2-3次→10分（有轮动），4+次→5分（快速轮动）
        if changes <= 1:
            rotation_speed_score = 15
        elif changes <= 3:
            rotation_speed_score = 10
        else:
            rotation_speed_score = 5

        # 5. 资金流向（10分）
        # 老板块炸板增加 + 新板块涨停 = 资金切换信号
        flow_score = 7  # 默认中性
        flow_signals = []
        if prev_concepts and faded_concepts:
            # 检查消退板块是否有炸板
            faded_with_break = [c for c in faded_concepts if c in break_concepts]
            if faded_with_break:
                flow_signals.append(f"资金从{', '.join(list(faded_with_break)[:3])}撤出")
                flow_score = 5
        if new_concepts and top_concept in new_concepts:
            flow_signals.append(f"新主线{top_concept}崛起")
            flow_score = max(flow_score, 8)
        if top_concept_days >= 3 and top_concept not in new_concepts:
            flow_signals.append(f"资金持续聚焦{top_concept}")
            flow_score = 9

        total_score = concentration_score + persist_score + switch_score + rotation_speed_score + flow_score

        # 判断轮动模式
        pattern = self._classify_rotation_pattern(
            top3_ratio, top_concept_days, new_ratio, changes,
            len(persistent_concepts_dict := concept_days), total_concept_stocks)

        return {
            'date': date,
            'score': total_score,
            'pattern': pattern,
            'pattern_name': ROTATION_PATTERNS[pattern]['name'],
            'pattern_emoji': ROTATION_PATTERNS[pattern]['emoji'],
            'pattern_desc': ROTATION_PATTERNS[pattern]['desc'],
            'metrics': {
                'top_concept': top_concept,
                'top_concept_count': today_concepts.get(top_concept, 0),
                'top_concept_days': top_concept_days,
                'top3_ratio': round(top3_ratio, 3),
                'top3_concepts': sorted_concepts[:3],
                'new_concepts': list(new_concepts),
                'new_concept_count': len(new_concepts),
                'new_concept_stocks': new_count,
                'new_ratio': round(new_ratio, 3),
                'faded_concepts': list(faded_concepts),
                'top1_history': top1_history,
                'top1_changes': changes,
                'concept_days': concept_days,
                'break_concepts': break_concepts,
            },
            'sub_scores': {
                'concentration': concentration_score,
                'persistence': persist_score,
                'switch': switch_score,
                'speed': rotation_speed_score,
                'flow': flow_score,
            },
            'signals': self._generate_rotation_signals(
                pattern, top_concept, top_concept_days, top3_ratio,
                new_concepts, faded_concepts, changes, flow_signals),
        }

    def _classify_rotation_pattern(self, top3_ratio, top_days, new_ratio,
                                    changes, persistent_count, total_stocks) -> str:
        """
        分类轮动模式。

        判断优先级（胜率第一，宁可判保守不判激进）：
        1. 主线主导：当前主线持续≥3天 + 集中度≥40%（不看历史changes，只看当前是否已形成主线）
        2. 无序轮动：新板块占比≥45%（大量新题材一日游），或主线仅1天+变化≥4
        3. 全面扩散：涨停≥40 + 集中度<35% + 新板块比例适度（普涨）
        4. 收缩防守：涨停<25 + 集中度≥55%
        5. 板块轮动：其余情况
        """
        # 主线主导：当前主线已持续≥3天且有一定集中度
        if top_days >= 3 and top3_ratio >= 0.38:
            return 'mainline'

        # 无序轮动：新板块大量涌现（资金无方向乱打）
        if new_ratio >= 0.45:
            return 'chaos'
        # 主线只持续1天且Top1频繁变化
        if top_days <= 1 and changes >= 4:
            return 'chaos'

        # 全面扩散：涨停多、分散、新板块比例不极端
        if total_stocks >= 40 and top3_ratio < 0.35 and new_ratio < 0.35:
            return 'diffusion'

        # 收缩防守
        if total_stocks < 25 and top3_ratio >= 0.55:
            return 'contraction'

        return 'rotation'

    def _empty_rotation_result(self, date: str) -> Dict:
        return {
            'date': date, 'score': 0,
            'pattern': 'chaos', 'pattern_name': '无序轮动',
            'pattern_emoji': '🌀', 'pattern_desc': '无数据',
            'metrics': {}, 'sub_scores': {},
            'signals': ['无涨停数据，无法判断轮动模式'],
        }

    def _generate_rotation_signals(self, pattern, top_concept, top_days,
                                    top3_ratio, new_concepts, faded_concepts,
                                    changes, flow_signals) -> List[str]:
        signals = []
        if pattern == 'mainline':
            signals.append(f"主线【{top_concept}】已持续{top_days}天，资金聚焦，重点操作主线龙头")
        elif pattern == 'rotation':
            signals.append(f"板块轮动中，主线{top_concept}，关注轮动到的新板块低位机会")
        elif pattern == 'diffusion':
            signals.append("资金全面扩散，普涨行情，持股待涨为主，不追高")
        elif pattern == 'contraction':
            signals.append("资金收缩防守，仅少数板块活跃，控制仓位")
        elif pattern == 'chaos':
            signals.append("板块无序切换，资金无方向，空仓等待主线明确")

        if new_concepts:
            signals.append(f"今日新出现板块: {', '.join(list(new_concepts)[:5])}")
        if faded_concepts:
            signals.append(f"今日消退板块: {', '.join(list(faded_concepts)[:5])}")

        signals.extend(flow_signals)
        return signals

    # ============================================================
    # 综合分析
    # ============================================================

    def analyze(self, date: str, save: bool = True) -> Dict[str, Any]:
        """
        执行完整的资金流分析。

        返回综合评估结果，包含：
        - attack: 进攻力度分析
        - persistence: 持续能力分析
        - rotation: 轮动习惯分析
        - composite_score: 综合评分
        - composite_level: 综合等级
        - operation_guidance: 操作指导
        """
        logger.info(f"[{date}] 开始资金流分析...")

        attack = self.analyze_attack(date)
        persistence = self.analyze_persistence(date)
        rotation = self.analyze_rotation(date)

        # 综合评分：进攻35% + 持续35% + 轮动30%
        composite = round(
            attack['score'] * 0.35 +
            persistence['score'] * 0.35 +
            rotation['score'] * 0.30, 1
        )

        # 综合等级与操作指导
        if composite >= 70:
            comp_level = 'aggressive'
            guidance = "资金面积极，可重仓参与主线龙头"
        elif composite >= 55:
            comp_level = 'positive'
            guidance = "资金面偏多，可适度参与确定性龙头"
        elif composite >= 40:
            comp_level = 'neutral'
            guidance = "资金面中性，小仓位试探或观望"
        elif composite >= 25:
            comp_level = 'cautious'
            guidance = "资金面偏弱，严格控制仓位，快进快出"
        else:
            comp_level = 'defensive'
            guidance = "资金面恶劣，空仓等待"

        # 额外的组合信号判断（结构化，供下游模块解析）
        combo_signals = []
        # 强攻 + 强持续 = 最佳操作窗口
        if attack['level'] == 'strong' and persistence['level'] == 'strong':
            combo_signals.append({
                'type': 'golden_window',
                'emoji': '🎯',
                'text': '强攻+强持续，黄金操作窗口，重仓龙头'
            })
        # 强攻 + 弱持续 = 一日游风险
        elif attack['level'] == 'strong' and persistence['level'] in ('weak', 'none'):
            combo_signals.append({
                'type': 'one_day_wonder',
                'emoji': '⚠️',
                'text': '强攻但持续性差，警惕一日游，只打板不追高'
            })
        # 弱攻 + 强持续 = 结构性行情
        elif attack['level'] in ('weak', 'defensive') and persistence['level'] == 'strong':
            combo_signals.append({
                'type': 'structural',
                'emoji': '🔄',
                'text': '指数弱但连板持续，结构性行情，聚焦龙头'
            })
        # 主线主导 + 强持续 = 满仓主线
        if rotation['pattern'] == 'mainline' and persistence['level'] in ('strong', 'moderate'):
            combo_signals.append({
                'type': 'mainline_focus',
                'emoji': '🎯',
                'text': f"主线【{rotation['metrics'].get('top_concept', '')}】持续中，重仓主线龙头"
            })
        # 无序轮动 = 空仓
        if rotation['pattern'] == 'chaos':
            combo_signals.append({
                'type': 'danger_zone',
                'emoji': '🛡️',
                'text': '无序轮动，不要试错，空仓等主线'
            })

        # 仓位建议系数（0-1），供operation_planner调整
        position_multiplier = self._calc_position_multiplier(
            attack, persistence, rotation)

        result = {
            'date': date,
            'composite_score': composite,
            'composite_level': comp_level,
            'guidance': guidance,
            'position_multiplier': position_multiplier,
            'attack': attack,
            'persistence': persistence,
            'rotation': rotation,
            'combo_signals': combo_signals,
            'analyzed_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }

        if save:
            self._save_result(date, result)

        logger.info(f"[{date}] 资金流分析完成: 综合{composite}分 "
                    f"进攻{attack['score']} 持续{persistence['score']} "
                    f"轮动{rotation['score']}")

        return result

    def _calc_position_multiplier(self, attack, persistence, rotation) -> float:
        """
        计算仓位建议系数（0-1.5），供操作计划调整基础仓位。

        胜率第一原则：
        - 所有维度均强 → 1.3（可适度加仓）
        - 主线明确+持续好 → 1.1
        - 中性 → 1.0（标准仓位）
        - 有一个维度弱 → 0.7
        - 无序轮动或无持续 → 0.4
        - 防守 → 0.2
        """
        mult = 1.0

        # 进攻调整
        if attack['level'] == 'strong':
            mult += 0.15
        elif attack['level'] == 'weak':
            mult -= 0.15
        elif attack['level'] == 'defensive':
            mult -= 0.3

        # 持续调整（权重更大）
        if persistence['level'] == 'strong':
            mult += 0.2
        elif persistence['level'] == 'weak':
            mult -= 0.2
        elif persistence['level'] == 'none':
            mult -= 0.35

        # 轮动调整
        if rotation['pattern'] == 'mainline':
            mult += 0.1
        elif rotation['pattern'] == 'chaos':
            mult -= 0.3
        elif rotation['pattern'] == 'contraction':
            mult -= 0.15
        elif rotation['pattern'] == 'diffusion':
            mult += 0.05

        return round(max(0.1, min(1.5, mult)), 2)

    # ============================================================
    # 数据库存储
    # ============================================================

    def init_tables(self):
        """创建资金流分析结果表"""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS capital_flow_analysis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                composite_score REAL,
                composite_level TEXT,
                guidance TEXT,
                position_multiplier REAL,
                attack_score REAL,
                attack_level TEXT,
                attack_metrics TEXT,
                persistence_score REAL,
                persistence_level TEXT,
                persistence_metrics TEXT,
                rotation_score REAL,
                rotation_pattern TEXT,
                rotation_metrics TEXT,
                combo_signals TEXT,
                full_result TEXT,
                analyzed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(date)
            );

            CREATE TABLE IF NOT EXISTS capital_flow_concept_tracking (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                concept TEXT NOT NULL,
                stock_count INTEGER,
                break_count INTEGER DEFAULT 0,
                consecutive_days INTEGER DEFAULT 1,
                is_new INTEGER DEFAULT 0,
                is_fading INTEGER DEFAULT 0,
                net_inflow_score REAL DEFAULT 0,
                UNIQUE(date, concept)
            );

            CREATE INDEX IF NOT EXISTS idx_cfa_date ON capital_flow_analysis(date);
            CREATE INDEX IF NOT EXISTS idx_cfct_date ON capital_flow_concept_tracking(date);
            CREATE INDEX IF NOT EXISTS idx_cfct_concept ON capital_flow_concept_tracking(concept);
        """)
        self.conn.commit()
        logger.info("资金流分析表初始化完成")

    def _save_result(self, date: str, result: Dict):
        """保存分析结果"""
        try:
            self.init_tables()
            self.conn.execute("""
                INSERT OR REPLACE INTO capital_flow_analysis
                (date, composite_score, composite_level, guidance, position_multiplier,
                 attack_score, attack_level, attack_metrics,
                 persistence_score, persistence_level, persistence_metrics,
                 rotation_score, rotation_pattern, rotation_metrics,
                 combo_signals, full_result)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                date,
                result['composite_score'],
                result['composite_level'],
                result['guidance'],
                result['position_multiplier'],
                result['attack']['score'],
                result['attack']['level'],
                json.dumps(result['attack']['metrics'], ensure_ascii=False),
                result['persistence']['score'],
                result['persistence']['level'],
                json.dumps(result['persistence']['metrics'], ensure_ascii=False),
                result['rotation']['score'],
                result['rotation']['pattern'],
                json.dumps(result['rotation']['metrics'], ensure_ascii=False),
                json.dumps(result['combo_signals'], ensure_ascii=False),
                json.dumps(result, ensure_ascii=False, default=str),
            ))

            # 保存板块追踪
            rotation = result['rotation']
            metrics = rotation.get('metrics', {})
            concept_days = metrics.get('concept_days', {})
            new_concepts = set(metrics.get('new_concepts', []))
            faded_concepts = set(metrics.get('faded_concepts', []))
            top3 = metrics.get('top3_concepts', [])
            break_concepts = metrics.get('break_concepts', {})

            today_concepts = self._get_concept_stats(date)
            for concept, count in today_concepts.items():
                if concept == '其他':
                    continue
                self.conn.execute("""
                    INSERT OR REPLACE INTO capital_flow_concept_tracking
                    (date, concept, stock_count, break_count, consecutive_days,
                     is_new, is_fading, net_inflow_score)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    date, concept, count,
                    break_concepts.get(concept, 0),
                    concept_days.get(concept, 1),
                    1 if concept in new_concepts else 0,
                    1 if concept in faded_concepts else 0,
                    # 净流入评分：涨停数-炸板数，归一化
                    round((count - break_concepts.get(concept, 0)) / max(count, 1), 2),
                ))

            self.conn.commit()
        except Exception as e:
            logger.error(f"保存资金流分析结果失败: {e}", exc_info=True)
            self.conn.rollback()

    # ============================================================
    # 报告输出
    # ============================================================

    def format_report(self, result: Dict) -> str:
        """格式化资金流分析报告"""
        lines = []
        lines.append("=" * 60)
        lines.append(f"💰 资金流分析报告 - {result['date']}")
        lines.append("=" * 60)

        # 综合评估
        lines.append(f"\n【综合评估】{result['composite_score']}分 | {result['guidance']}")
        lines.append(f"  仓位系数: {result['position_multiplier']}x")

        # 进攻
        a = result['attack']
        lines.append(f"\n{'─' * 40}")
        lines.append(f"⚔️ 进攻力度: {a['score']}分 {a['level_emoji']}{a['level_name']}")
        lines.append(f"  {a['level_desc']}")
        m = a.get('metrics', {})
        if m:
            lines.append(f"  涨停{m.get('total_limit_up', 0)}家 炸板{m.get('break_count', 0)}家 "
                        f"封板率{m.get('seal_rate', 0):.0%}")
            lines.append(f"  早盘涨停占比{m.get('early_ratio', 0):.0%} "
                        f"连板占比{m.get('lianban_ratio', 0):.0%}")
            if m.get('rise_count'):
                lines.append(f"  涨{m['rise_count']}家 跌{m['fall_count']}家 "
                            f"广度{m.get('breadth', 0):.0%}")
        for s in a.get('signals', []):
            lines.append(f"  • {s}")

        # 持续
        p = result['persistence']
        lines.append(f"\n{'─' * 40}")
        lines.append(f"🔗 持续能力: {p['score']}分 {p['level_emoji']}{p['level_name']}")
        lines.append(f"  {p['level_desc']}")
        m = p.get('metrics', {})
        if m:
            lines.append(f"  晋级率{m.get('avg_promotion_rate', 0):.0%} "
                        f"最高{m.get('today_max_boards', 0)}板 "
                        f"高标存活{m.get('high_survival_rate', 0):.0%}")
            pc = m.get('persistent_concepts', {})
            if pc:
                top_persist = sorted(pc.items(), key=lambda x: -x[1])[:3]
                lines.append(f"  持续板块: {', '.join([f'{c}({d}天)' for c,d in top_persist])}")
        for s in p.get('signals', []):
            lines.append(f"  • {s}")

        # 轮动
        r = result['rotation']
        lines.append(f"\n{'─' * 40}")
        lines.append(f"🔄 轮动模式: {r['score']}分 {r['pattern_emoji']}{r['pattern_name']}")
        lines.append(f"  {r['pattern_desc']}")
        m = r.get('metrics', {})
        if m:
            lines.append(f"  主线: {m.get('top_concept', '')}({m.get('top_concept_count', 0)}家 "
                        f"持续{m.get('top_concept_days', 0)}天)")
            lines.append(f"  Top3集中度{m.get('top3_ratio', 0):.0%} "
                        f"新板块占比{m.get('new_ratio', 0):.0%}")
        for s in r.get('signals', []):
            lines.append(f"  • {s}")

        # 组合信号
        if result.get('combo_signals'):
            lines.append(f"\n{'─' * 40}")
            lines.append("🎯 组合信号:")
            for s in result['combo_signals']:
                if isinstance(s, dict):
                    lines.append(f"  {s.get('emoji', '')} {s.get('text', '')}")
                else:
                    lines.append(f"  {s}")

        lines.append("\n" + "=" * 60)
        return '\n'.join(lines)

    def get_historical_scores(self, days: int = 20) -> List[Dict]:
        """获取历史资金流评分趋势"""
        rows = self.conn.execute(
            "SELECT date, composite_score, attack_score, persistence_score, "
            "rotation_score, composite_level, position_multiplier "
            "FROM capital_flow_analysis ORDER BY date DESC LIMIT ?",
            (days,)
        ).fetchall()
        return [dict(r) for r in rows]


# ============================================================
# 命令行入口
# ============================================================

def analyze_capital_flow(date: str = None, db_path: str = DB_PATH):
    """命令行入口"""
    logging.basicConfig(level=logging.INFO, format='%(message)s')

    if date is None:
        # 默认取最新交易日
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT MAX(date) FROM xgt_limit_up_detail"
        ).fetchone()
        date = row[0] if row else None
        conn.close()

    if not date:
        print("无可用交易数据")
        return

    analyzer = CapitalFlowAnalyzer(db_path)
    try:
        result = analyzer.analyze(date)
        print(analyzer.format_report(result))
    finally:
        analyzer.close()


if __name__ == '__main__':
    import sys
    target_date = sys.argv[1] if len(sys.argv) > 1 else None
    analyze_capital_flow(target_date)
