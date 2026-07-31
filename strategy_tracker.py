"""
strategy_tracker.py - 策略跟踪与自我维护模块
跟踪实盘策略执行情况，自动评估信号有效性，动态调整参数
"""

import sqlite3
import json
from datetime import datetime
from collections import defaultdict

DB_PATH = "/app/data/所有对话/主对话/stock_data_1784791326780_0_09ym.db"

# 信号元数据
SIGNAL_META = {
    1: {'name': '5→6突破+砸盘下降', 'stars': 3, 'direction': 'bullish'},
    2: {'name': '砸盘骤降>3+连板≤3', 'stars': 3, 'direction': 'bullish'},
    3: {'name': '连续2天砸盘<3+连板≤3', 'stars': 2, 'direction': 'bullish'},
    4: {'name': '7板+砸盘>6', 'stars': 2, 'direction': 'bearish'},
    5: {'name': '4板+涨停数<35+砸盘<3', 'stars': 1, 'direction': 'bearish'},
}


class StrategyTracker:
    """策略跟踪器"""
    
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_tables()
    
    def _init_tables(self):
        """初始化所需的数据库表"""
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS strategy_tracking (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                signal_id INTEGER NOT NULL,
                triggered INTEGER NOT NULL DEFAULT 0,
                details TEXT,
                verified INTEGER DEFAULT 0,
                actual_result TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS parameter_adjustments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                parameter_name TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # 信号权重表
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS signal_weights (
                signal_id INTEGER PRIMARY KEY,
                weight REAL DEFAULT 1.0,
                trigger_threshold REAL DEFAULT 1.0,
                consecutive_success INTEGER DEFAULT 0,
                consecutive_failure INTEGER DEFAULT 0,
                total_triggers INTEGER DEFAULT 0,
                total_correct INTEGER DEFAULT 0,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # 初始化权重
        for sig_id in range(1, 6):
            self.conn.execute("""
                INSERT OR IGNORE INTO signal_weights (signal_id, weight, trigger_threshold)
                VALUES (?, 1.0, 1.0)
            """, (sig_id,))
        self.conn.commit()
    
    def close(self):
        self.conn.close()
    
    # ==================== 核心方法 ====================
    
    def record_signal_trigger(self, date_str, signal_id, details=None, triggered=True):
        """
        记录信号触发
        
        Args:
            date_str: 触发日期 (YYYY-MM-DD)
            signal_id: 信号ID (1~5)
            details: 触发条件详情 (dict)
            triggered: 是否触发 (bool)
        """
        self.conn.execute("""
            INSERT INTO strategy_tracking (date, signal_id, triggered, details)
            VALUES (?, ?, ?, ?)
        """, (
            date_str,
            signal_id,
            1 if triggered else 0,
            json.dumps(details or {}, ensure_ascii=False),
        ))
        self.conn.commit()
        
        # 更新权重表的触发计数
        if triggered:
            self.conn.execute("""
                UPDATE signal_weights 
                SET total_triggers = total_triggers + 1, last_updated = CURRENT_TIMESTAMP
                WHERE signal_id = ?
            """, (signal_id,))
            self.conn.commit()
    
    def verify_signal(self, signal_id, trigger_date):
        """
        用次日数据验证信号是否正确
        
        Args:
            signal_id: 信号ID
            trigger_date: 触发日期
        
        Returns:
            dict: 验证结果
        """
        # 获取次日数据
        next_date = self._get_next_trading_day(trigger_date)
        if not next_date:
            return {'verified': False, 'reason': '无次日数据'}
        
        # 获取触发日和次日市场数据
        curr_data = self._get_market_data(trigger_date)
        next_data = self._get_market_data(next_date)
        
        if not curr_data or not next_data:
            return {'verified': False, 'reason': '数据不完整'}
        
        # 根据信号方向判断是否正确
        direction = SIGNAL_META[signal_id]['direction']
        lu_change = next_data['total_limit_up'] - curr_data['total_limit_up']
        boards_change = next_data['max_boards'] - curr_data['max_boards']
        
        if direction == 'bullish':
            is_correct = (lu_change > 0) or (boards_change > 0)
        else:
            is_correct = (lu_change < 0) or (boards_change < 0)
        
        actual_result = {
            'next_date': next_date,
            'next_total_limit_up': next_data['total_limit_up'],
            'next_max_boards': next_data['max_boards'],
            'next_sc': next_data['smash_coefficient'],
            'limit_up_change': lu_change,
            'boards_change': boards_change,
        }
        
        # 更新数据库
        self.conn.execute("""
            UPDATE strategy_tracking 
            SET verified = 1, actual_result = ?
            WHERE signal_id = ? AND date = ?
        """, (
            json.dumps(actual_result, ensure_ascii=False),
            signal_id,
            trigger_date,
        ))
        
        # 更新连续成功/失败计数
        self._update_consecutive_counts(signal_id, is_correct)
        
        self.conn.commit()
        
        return {
            'verified': True,
            'correct': is_correct,
            'actual_result': actual_result,
        }
    
    def get_signal_stats(self, signal_id=None):
        """
        获取信号统计
        
        Args:
            signal_id: 指定信号ID，None则返回所有信号统计
        
        Returns:
            dict: 统计信息
        """
        if signal_id:
            return self._get_single_signal_stats(signal_id)
        
        # 所有信号统计
        stats = {}
        for sid in range(1, 6):
            stats[sid] = self._get_single_signal_stats(sid)
        
        # 汇总
        total_triggers = sum(s['total_triggers'] for s in stats.values())
        total_correct = sum(s['correct'] for s in stats.values())
        
        return {
            'by_signal': stats,
            'summary': {
                'total_triggers': total_triggers,
                'total_correct': total_correct,
                'overall_hit_rate': round(total_correct / total_triggers * 100, 1) if total_triggers > 0 else 0,
            }
        }
    
    def auto_adjust_parameters(self):
        """
        根据近期信号表现自动调整参数
        
        规则:
        - 连续3次失败: 降低权重(×0.8), 提高触发阈值(×1.2)
        - 连续3次成功: 提高权重(×1.2), 降低触发阈值(×0.9)
        """
        adjustments = []
        today = datetime.now().strftime('%Y-%m-%d')
        
        for sig_id in range(1, 6):
            row = self.conn.execute("""
                SELECT weight, trigger_threshold, consecutive_success, consecutive_failure
                FROM signal_weights WHERE signal_id = ?
            """, (sig_id,)).fetchone()
            
            if not row:
                continue
            
            weight = row['weight']
            threshold = row['trigger_threshold']
            cs = row['consecutive_success']
            cf = row['consecutive_failure']
            
            old_weight = weight
            old_threshold = threshold
            reason = ''
            
            # 连续3次失败 → 降低权重/提高阈值
            if cf >= 3:
                new_weight = round(weight * 0.8, 4)
                new_threshold = round(threshold * 1.2, 4)
                reason = f'信号{sig_id}连续{cf}次验证失败，降低权重{weight}→{new_weight}，提高阈值{threshold}→{new_threshold}'
                
                self.conn.execute("""
                    UPDATE signal_weights 
                    SET weight = ?, trigger_threshold = ?, consecutive_failure = 0,
                        last_updated = CURRENT_TIMESTAMP
                    WHERE signal_id = ?
                """, (new_weight, new_threshold, sig_id))
                
                adjustments.append({
                    'signal_id': sig_id,
                    'parameter': 'weight',
                    'old': old_weight,
                    'new': new_weight,
                    'reason': reason,
                })
                adjustments.append({
                    'signal_id': sig_id,
                    'parameter': 'threshold',
                    'old': old_threshold,
                    'new': new_threshold,
                    'reason': reason,
                })
            
            # 连续3次成功 → 提高权重/降低阈值
            elif cs >= 3:
                new_weight = round(min(weight * 1.2, 3.0), 4)  # 上限3.0
                new_threshold = round(max(threshold * 0.9, 0.5), 4)  # 下限0.5
                reason = f'信号{sig_id}连续{cs}次验证成功，提高权重{weight}→{new_weight}，降低阈值{threshold}→{new_threshold}'
                
                self.conn.execute("""
                    UPDATE signal_weights 
                    SET weight = ?, trigger_threshold = ?, consecutive_success = 0,
                        last_updated = CURRENT_TIMESTAMP
                    WHERE signal_id = ?
                """, (new_weight, new_threshold, sig_id))
                
                adjustments.append({
                    'signal_id': sig_id,
                    'parameter': 'weight',
                    'old': old_weight,
                    'new': new_weight,
                    'reason': reason,
                })
                adjustments.append({
                    'signal_id': sig_id,
                    'parameter': 'threshold',
                    'old': old_threshold,
                    'new': new_threshold,
                    'reason': reason,
                })
        
        # 保存调整日志
        for adj in adjustments:
            self.conn.execute("""
                INSERT INTO parameter_adjustments (date, parameter_name, old_value, new_value, reason)
                VALUES (?, ?, ?, ?, ?)
            """, (
                today,
                f"signal_{adj['signal_id']}_{adj['parameter']}",
                str(adj['old']),
                str(adj['new']),
                adj['reason'],
            ))
        
        self.conn.commit()
        return adjustments
    
    def generate_strategy_report(self):
        """
        生成策略运行报告
        
        Returns:
            dict: 完整报告
        """
        stats = self.get_signal_stats()
        
        # 获取最近的参数调整
        adjustments = self.conn.execute("""
            SELECT * FROM parameter_adjustments 
            ORDER BY created_at DESC LIMIT 20
        """).fetchall()
        
        # 获取当前权重
        weights = {}
        for sig_id in range(1, 6):
            row = self.conn.execute("""
                SELECT weight, trigger_threshold, consecutive_success, consecutive_failure,
                       total_triggers, total_correct
                FROM signal_weights WHERE signal_id = ?
            """, (sig_id,)).fetchone()
            if row:
                weights[sig_id] = dict(row)
        
        report = {
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
            'signal_stats': stats,
            'current_weights': weights,
            'recent_adjustments': [dict(a) for a in adjustments[:10]],
            'recommendations': self._generate_recommendations(stats, weights),
        }
        
        return report
    
    # ==================== 内部方法 ====================
    
    def _get_single_signal_stats(self, signal_id):
        """获取单个信号的统计"""
        # 总触发次数
        total = self.conn.execute("""
            SELECT COUNT(*) as cnt FROM strategy_tracking
            WHERE signal_id = ? AND triggered = 1
        """, (signal_id,)).fetchone()['cnt']
        
        # 已验证次数和正确次数
        verified = self.conn.execute("""
            SELECT COUNT(*) as cnt FROM strategy_tracking
            WHERE signal_id = ? AND triggered = 1 AND verified = 1
        """, (signal_id,)).fetchone()['cnt']
        
        correct = 0
        if verified > 0:
            rows = self.conn.execute("""
                SELECT actual_result FROM strategy_tracking
                WHERE signal_id = ? AND triggered = 1 AND verified = 1
            """, (signal_id,)).fetchall()
            
            direction = SIGNAL_META[signal_id]['direction']
            for row in rows:
                try:
                    ar = json.loads(row['actual_result'])
                    lu_change = ar.get('limit_up_change', 0)
                    boards_change = ar.get('boards_change', 0)
                    if direction == 'bullish':
                        if lu_change > 0 or boards_change > 0:
                            correct += 1
                    else:
                        if lu_change < 0 or boards_change < 0:
                            correct += 1
                except (json.JSONDecodeError, TypeError):
                    pass
        
        # 最近5次触发
        recent = self.conn.execute("""
            SELECT date, details, verified, actual_result
            FROM strategy_tracking
            WHERE signal_id = ? AND triggered = 1
            ORDER BY date DESC LIMIT 5
        """, (signal_id,)).fetchall()
        
        return {
            'signal_id': signal_id,
            'name': SIGNAL_META[signal_id]['name'],
            'stars': SIGNAL_META[signal_id]['stars'],
            'total_triggers': total,
            'verified_count': verified,
            'correct': correct,
            'hit_rate': round(correct / verified * 100, 1) if verified > 0 else 0,
            'recent_triggers': [dict(r) for r in recent],
        }
    
    def _update_consecutive_counts(self, signal_id, is_correct):
        """更新连续成功/失败计数"""
        if is_correct:
            self.conn.execute("""
                UPDATE signal_weights 
                SET consecutive_success = consecutive_success + 1,
                    consecutive_failure = 0,
                    total_correct = total_correct + 1,
                    last_updated = CURRENT_TIMESTAMP
                WHERE signal_id = ?
            """, (signal_id,))
        else:
            self.conn.execute("""
                UPDATE signal_weights 
                SET consecutive_failure = consecutive_failure + 1,
                    consecutive_success = 0,
                    last_updated = CURRENT_TIMESTAMP
                WHERE signal_id = ?
            """, (signal_id,))
    
    def _get_next_trading_day(self, date_str):
        """获取下一个交易日"""
        row = self.conn.execute("""
            SELECT DISTINCT date FROM akshare_limit_up
            WHERE date > ? AND date >= '2026-01-21'
            ORDER BY date LIMIT 1
        """, (date_str,)).fetchone()
        return row[0] if row else None
    
    def _get_market_data(self, date_str):
        """获取某日市场汇总数据"""
        smash = self.conn.execute("""
            SELECT smash_coefficient, max_continuous_boards
            FROM smash_coefficient_results WHERE date = ?
        """, (date_str,)).fetchone()
        
        total = self.conn.execute("""
            SELECT COUNT(*) as cnt FROM akshare_limit_up WHERE date = ?
        """, (date_str,)).fetchone()
        
        if not smash or not total:
            return None
        
        return {
            'date': date_str,
            'smash_coefficient': smash['smash_coefficient'],
            'max_boards': smash['max_continuous_boards'],
            'total_limit_up': total['cnt'],
        }
    
    def _generate_recommendations(self, stats, weights):
        """根据策略数据生成建议"""
        recommendations = []
        
        for sig_id in range(1, 6):
            s = stats['by_signal'][sig_id]
            w = weights.get(sig_id, {})
            
            if s['total_triggers'] == 0:
                recommendations.append({
                    'signal_id': sig_id,
                    'status': '未触发',
                    'message': f"信号{s['name']}在跟踪期间未触发",
                })
                continue
            
            hit_rate = s['hit_rate']
            
            if hit_rate >= 80:
                recommendations.append({
                    'signal_id': sig_id,
                    'status': '优秀',
                    'message': f"信号{s['name']}命中率{hit_rate}%，表现优秀，可加大权重",
                    'current_weight': w.get('weight', 1.0),
                })
            elif hit_rate >= 60:
                recommendations.append({
                    'signal_id': sig_id,
                    'status': '良好',
                    'message': f"信号{s['name']}命中率{hit_rate}%，表现稳定",
                    'current_weight': w.get('weight', 1.0),
                })
            elif hit_rate >= 40:
                recommendations.append({
                    'signal_id': sig_id,
                    'status': '一般',
                    'message': f"信号{s['name']}命中率{hit_rate}%，需关注近期表现",
                    'current_weight': w.get('weight', 1.0),
                })
            else:
                recommendations.append({
                    'signal_id': sig_id,
                    'status': '较差',
                    'message': f"信号{s['name']}命中率{hit_rate}%，建议降低权重或调整参数",
                    'current_weight': w.get('weight', 1.0),
                })
        
        return recommendations


def format_strategy_report(report):
    """格式化策略报告"""
    lines = []
    lines.append("=" * 70)
    lines.append("📊 策略运行报告")
    lines.append(f"生成时间: {report['generated_at']}")
    lines.append("=" * 70)
    
    # 信号统计
    lines.append("\n📡 信号统计:")
    lines.append("─" * 60)
    
    stats = report['signal_stats']
    for sig_id in range(1, 6):
        s = stats['by_signal'][sig_id]
        stars = '⭐' * s['stars']
        lines.append(f"  信号{sig_id} {stars}: {s['name']}")
        lines.append(f"    触发: {s['total_triggers']}次 | 验证: {s['verified_count']}次 | "
                     f"正确: {s['correct']}次 | 命中率: {s['hit_rate']}%")
    
    summary = stats['summary']
    lines.append(f"\n  📈 总体: 触发{summary['total_triggers']}次, "
                 f"正确{summary['total_correct']}次, "
                 f"命中率{summary['overall_hit_rate']}%")
    
    # 当前权重
    lines.append(f"\n\n⚖️ 当前信号权重:")
    lines.append("─" * 60)
    for sig_id in range(1, 6):
        w = report['current_weights'].get(sig_id, {})
        if w:
            cs = w.get('consecutive_success', 0)
            cf = w.get('consecutive_failure', 0)
            lines.append(f"  信号{sig_id}: 权重={w.get('weight', 1.0)}, "
                        f"阈值={w.get('trigger_threshold', 1.0)}, "
                        f"连续成功={cs}, 连续失败={cf}")
    
    # 建议
    lines.append(f"\n\n💡 策略建议:")
    lines.append("─" * 60)
    for rec in report['recommendations']:
        status_icon = {'优秀': '🏆', '良好': '✅', '一般': '⚠️', '较差': '❌', '未触发': '💤'}.get(rec['status'], '')
        lines.append(f"  {status_icon} 信号{rec['signal_id']}: [{rec['status']}] {rec['message']}")
    
    # 调整记录
    if report['recent_adjustments']:
        lines.append(f"\n\n📝 最近参数调整:")
        lines.append("─" * 60)
        for adj in report['recent_adjustments'][:5]:
            lines.append(f"  [{adj.get('date', '')}] {adj['parameter_name']}: "
                        f"{adj['old_value']} → {adj['new_value']}")
            lines.append(f"    原因: {adj['reason']}")
    
    lines.append("\n" + "=" * 70)
    return '\n'.join(lines)


def run_historical_tracking():
    """
    从历史数据中回补策略跟踪记录
    用backtester的逻辑检查67天数据，记录所有信号触发
    """
    tracker = StrategyTracker()
    
    # 加载数据（复用backtester的逻辑）
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    # 获取所有交易日
    days = conn.execute("""
        SELECT DISTINCT date FROM akshare_limit_up
        WHERE date >= '2026-01-21' AND date <= '2026-05-11'
        ORDER BY date
    """).fetchall()
    days = [d[0] for d in days]
    
    print(f"开始回补策略跟踪记录，共 {len(days)} 个交易日...")
    
    signal_count = 0
    
    for i, date_str in enumerate(days):
        # 获取当日和前日数据
        curr_smash = conn.execute("""
            SELECT smash_coefficient, max_continuous_boards
            FROM smash_coefficient_results WHERE date = ?
        """, (date_str,)).fetchone()
        
        curr_count = conn.execute("""
            SELECT COUNT(*) as cnt FROM akshare_limit_up WHERE date = ?
        """, (date_str,)).fetchone()['cnt']
        
        if not curr_smash or i == 0:
            continue
        
        prev_date = days[i - 1]
        prev_smash = conn.execute("""
            SELECT smash_coefficient, max_continuous_boards
            FROM smash_coefficient_results WHERE date = ?
        """, (prev_date,)).fetchone()
        
        prev_count = conn.execute("""
            SELECT COUNT(*) as cnt FROM akshare_limit_up WHERE date = ?
        """, (prev_date,)).fetchone()['cnt']
        
        sc = curr_smash['smash_coefficient']
        mb = curr_smash['max_continuous_boards']
        prev_sc = prev_smash['smash_coefficient'] if prev_smash else 0
        prev_mb = prev_smash['max_continuous_boards'] if prev_smash else 0
        
        # 检查信号1: 5→6突破+砸盘下降
        if prev_mb == 5 and mb >= 6 and sc < prev_sc:
            tracker.record_signal_trigger(date_str, 1, {
                'prev_max_boards': prev_mb, 'curr_max_boards': mb,
                'sc_prev': prev_sc, 'sc_curr': sc,
            })
            signal_count += 1
        
        # 检查信号2: 砸盘骤降>3+连板≤3
        sc_drop = prev_sc - sc
        if sc_drop > 3 and mb <= 3:
            tracker.record_signal_trigger(date_str, 2, {
                'sc_prev': prev_sc, 'sc_curr': sc, 'sc_drop': round(sc_drop, 2),
                'max_boards': mb,
            })
            signal_count += 1
        
        # 检查信号3: 连续2天砸盘<3+连板≤3
        if sc < 3 and prev_sc < 3 and mb <= 3 and prev_mb <= 3:
            tracker.record_signal_trigger(date_str, 3, {
                'sc_today': sc, 'sc_prev': prev_sc,
                'max_boards_today': mb, 'max_boards_prev': prev_mb,
            })
            signal_count += 1
        
        # 检查信号4: 7板+砸盘>6
        if mb >= 7 and sc > 6:
            tracker.record_signal_trigger(date_str, 4, {
                'max_boards': mb, 'sc': sc,
            })
            signal_count += 1
        
        # 检查信号5: 4板+涨停数<35+砸盘<3
        if mb == 4 and curr_count < 35 and sc < 3:
            tracker.record_signal_trigger(date_str, 5, {
                'max_boards': mb, 'total_limit_up': curr_count, 'sc': sc,
            })
            signal_count += 1
    
    conn.close()
    
    print(f"共记录 {signal_count} 次信号触发")
    
    # 验证所有信号
    print("\n正在验证信号...")
    verify_count = 0
    for sig_id in range(1, 6):
        rows = tracker.conn.execute("""
            SELECT date FROM strategy_tracking
            WHERE signal_id = ? AND triggered = 1 AND verified = 0
            ORDER BY date
        """, (sig_id,)).fetchall()
        
        for row in rows:
            result = tracker.verify_signal(sig_id, row['date'])
            if result['verified']:
                verify_count += 1
    
    print(f"共验证 {verify_count} 次信号")
    
    # 自动调整参数
    print("\n正在自动调整参数...")
    adjustments = tracker.auto_adjust_parameters()
    if adjustments:
        for adj in adjustments:
            print(f"  调整: {adj['reason']}")
    else:
        print("  无需调整")
    
    # 生成报告
    report = tracker.generate_strategy_report()
    
    tracker.close()
    return report


if __name__ == '__main__':
    print("=" * 70)
    print("策略跟踪模块 - 历史数据回补与分析")
    print("=" * 70)
    
    report = run_historical_tracking()
    print("\n" + format_strategy_report(report))
