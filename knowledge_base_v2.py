"""
knowledge_base_v2.py - 新版知识库管理模块
核心改动：
1. 初始化时自动注入5个高价值信号作为规则知识
2. 新增 match_signals(date_str) 方法，直接调用cycle_model的信号检测
3. 知识衰减逻辑改为：信号的衰减基于实盘验证结果，而非时间
"""
import logging
import json
import os
import sqlite3
from datetime import datetime

logger = logging.getLogger(__name__)


class KnowledgeBaseV2:
    """知识库管理器V2"""
    
    # 5个高价值信号定义（自动注入）
    BUILTIN_SIGNALS = [
        {
            "signal_id": 1,
            "name": "5→6突破+砸盘下降",
            "pattern_type": "prediction_signal",
            "description": "连板从5板突破至6板且砸盘系数下降，次日涨停数预期77.8",
            "success_rate": 1.00,  # 历史3/3
            "occurrence_count": 3,
            "verification_basis": "历史3次触发，次日涨停数均值77.8",
            "decay_mode": "verification",  # 基于实盘验证衰减
        },
        {
            "signal_id": 2,
            "name": "砸盘骤降>3+连板≤3",
            "pattern_type": "prediction_signal",
            "description": "砸盘系数单日骤降超过3点且连板高度不超过3，见底反弹信号",
            "success_rate": 0.83,  # 历史83%
            "occurrence_count": 6,
            "verification_basis": "历史6次触发，83%在3日内出现反弹",
            "decay_mode": "verification",
        },
        {
            "signal_id": 3,
            "name": "连续2天砸盘<3+连板≤3",
            "pattern_type": "prediction_signal",
            "description": "连续2个交易日砸盘系数低于3且连板不超过3，底部确认信号",
            "success_rate": 0.75,  # 历史75%
            "occurrence_count": 4,
            "verification_basis": "历史4次触发，75%在5日内启动新一轮周期",
            "decay_mode": "verification",
        },
        {
            "signal_id": 4,
            "name": "7板+砸盘>6",
            "pattern_type": "prediction_signal",
            "description": "连板高度达到7板且砸盘系数超过6，见顶崩塌信号",
            "success_rate": 1.00,  # 历史100%
            "occurrence_count": 2,
            "verification_basis": "历史2次触发，100%次日连板高度骤降",
            "decay_mode": "verification",
        },
        {
            "signal_id": 5,
            "name": "4板+涨停<35+砸盘<3",
            "pattern_type": "prediction_signal",
            "description": "连板仅到4板且涨停数不足35只且砸盘系数低于3，假突破预警",
            "success_rate": 0.67,  # 历史67%
            "occurrence_count": 3,
            "verification_basis": "历史3次触发，67%确认假突破",
            "decay_mode": "verification",
        },
    ]
    
    def __init__(self, db, knowledge_dir=None, db_path=None):
        """
        初始化知识库V2
        db: 数据库连接对象
        knowledge_dir: 知识库目录
        db_path: sqlite数据库路径
        """
        self.db = db
        self.db_path = db_path or getattr(db, 'db_path', None)
        self.knowledge_dir = knowledge_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "knowledge")
        os.makedirs(self.knowledge_dir, exist_ok=True)
        
        # 初始化周期模型引用
        from cycle_model import CycleModel
        self.cycle_model = CycleModel(self.db_path)
        
        # 自动注入内置信号
        self._inject_builtin_signals()
    
    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    def _inject_builtin_signals(self):
        """自动注入5个高价值信号作为规则知识"""
        try:
            for sig in self.BUILTIN_SIGNALS:
                # 检查是否已存在
                existing = self.db.get_knowledge("prediction_signal")
                if existing:
                    existing_list = [dict(e) for e in existing]
                    already_exists = any(
                        f"信号{sig['signal_id']}" in e.get("description", "") or
                        sig["name"] in e.get("description", "")
                        for e in existing_list
                    )
                    if already_exists:
                        continue
                
                # 注入
                desc = f"信号{sig['signal_id']}: {sig['name']} - {sig['description']}"
                metadata = {
                    "signal_id": sig["signal_id"],
                    "success_rate": sig["success_rate"],
                    "occurrence_count": sig["occurrence_count"],
                    "verification_basis": sig["verification_basis"],
                    "decay_mode": sig["decay_mode"],
                }
                
                self.db.save_knowledge(
                    pattern_type="prediction_signal",
                    description=desc,
                    metadata=metadata
                )
                logger.info(f"注入内置信号: {sig['name']}")
        
        except Exception as e:
            logger.warning(f"注入内置信号异常: {e}")
    
    def match_signals(self, date_str):
        """
        检测当日是否触发5个高价值预测信号
        直接调用cycle_model的信号检测
        
        返回: [{
            "signal_id": int,
            "name": str,
            "triggered": bool,
            "details": str,
            "strength": int,
            "historical_success_rate": float,
            "action": str,
        }]
        """
        raw_signals = self.cycle_model.detect_signals(date_str)
        
        # 为每个信号补充历史成功率和操作建议
        signal_actions = {
            1: "次日涨停预期77.8，可积极加仓主线龙头",
            2: "见底反弹信号，可开始建仓低位龙头",
            3: "底部确认信号，可逐步建仓",
            4: "见顶崩塌信号，立即减仓或清仓",
            5: "假突破预警，不宜追高，减仓观望",
        }
        
        enriched_signals = []
        for sig in raw_signals:
            sid = sig.get("signal_id", 0)
            builtin = next((s for s in self.BUILTIN_SIGNALS if s["signal_id"] == sid), None)
            
            enriched = {
                **sig,
                "historical_success_rate": builtin["success_rate"] if builtin else 0.5,
                "historical_occurrences": builtin["occurrence_count"] if builtin else 0,
                "action": signal_actions.get(sid, "观望"),
            }
            enriched_signals.append(enriched)
        
        return enriched_signals
    
    # ============ 知识CRUD（与原版本一致） ============
    
    def add_pattern(self, pattern_type, description, metadata=None):
        """添加新知识"""
        self.db.save_knowledge(pattern_type, description, metadata)
        logger.info(f"新增知识: [{pattern_type}] {description}")
        self._sync_to_json()
    
    def get_patterns(self, pattern_type=None):
        """获取知识列表"""
        rows = self.db.get_knowledge(pattern_type)
        return [dict(r) for r in rows]
    
    def search_similar(self, pattern_type, keywords, threshold=0.5):
        """搜索相似知识"""
        all_knowledge = self.get_patterns(pattern_type)
        results = []
        for k in all_knowledge:
            desc = k.get("description", "")
            match_count = sum(1 for kw in keywords if kw in desc)
            if match_count > 0:
                score = match_count / len(keywords)
                if score >= threshold:
                    results.append({"knowledge": k, "score": score})
        
        return sorted(results, key=lambda x: x["score"], reverse=True)
    
    def update_pattern(self, knowledge_id, **kwargs):
        """更新知识"""
        updates = []
        params = []
        for key, value in kwargs.items():
            if key in ("success_rate", "last_verified", "occurrence_count", "metadata"):
                updates.append(f"{key} = ?")
                if key == "metadata":
                    params.append(json.dumps(value))
                else:
                    params.append(value)
        
        if updates:
            params.append(knowledge_id)
            self.db.execute(
                f"UPDATE market_knowledge SET {', '.join(updates)} WHERE id = ?",
                params)
            self._sync_to_json()
    
    # ============ 模式匹配 ============
    
    def match_current_pattern(self, analysis_result, pattern_result):
        """
        将当前市场状态与历史模式匹配
        """
        current_features = self._extract_features(analysis_result, pattern_result)
        
        similar = self.search_similar("market_state", current_features["keywords"])
        
        cycle_phase = pattern_result.get("cycle_phase", "") if pattern_result else ""
        cycle_knowledge = self.get_patterns("cycle_phase")
        matching_cycles = [k for k in cycle_knowledge if cycle_phase in k.get("description", "")]
        
        result = {
            "current_state": current_features,
            "similar_historical": similar[:3],
            "cycle_knowledge": matching_cycles[:3],
            "advice": self._generate_advice_from_knowledge(current_features, similar, matching_cycles),
        }
        return result
    
    def _extract_features(self, analysis, pattern):
        """从分析和模式结果中提取特征"""
        basic = analysis.get("basic_stats", {}) if analysis else {}
        sentiment = analysis.get("sentiment_score", 50) if analysis else 50
        
        features = {
            "sentiment_level": "高" if sentiment >= 60 else ("中" if sentiment >= 40 else "低"),
            "board_level": "高" if basic.get("max_boards", 0) >= 5 else ("中" if basic.get("max_boards", 0) >= 3 else "低"),
            "count_level": "多" if basic.get("total_count", 0) >= 70 else ("中" if basic.get("total_count", 0) >= 40 else "少"),
            "cycle_phase": pattern.get("cycle_phase", "未知") if pattern else "未知",
            "keywords": [],
        }
        
        keywords = []
        if sentiment >= 60:
            keywords.append("高情绪")
        if basic.get("max_boards", 0) >= 5:
            keywords.append("高连板")
        if basic.get("total_count", 0) >= 70:
            keywords.append("大量涨停")
        if basic.get("total_count", 0) < 30:
            keywords.append("缩量")
        
        phase = features["cycle_phase"]
        if phase in ("启动期", "发酵期"):
            keywords.append("上升周期")
        elif phase in ("退潮期", "冰点期"):
            keywords.append("下降周期")
        elif phase == "反包期":
            keywords.append("反包")
        
        features["keywords"] = keywords
        return features
    
    def _generate_advice_from_knowledge(self, features, similar, cycles):
        """基于知识库生成建议"""
        advice_parts = []
        
        for c in cycles:
            rate = c.get("success_rate", 0.5)
            if rate > 0.6:
                advice_parts.append(f"历史经验({c['description']}, 成功率{rate*100:.0f}%)")
        
        for s in similar[:2]:
            k = s["knowledge"]
            advice_parts.append(f"相似情境: {k['description']}(匹配度{s['score']*100:.0f}%)")
        
        if not advice_parts:
            return "知识库中暂无直接匹配的历史经验，建议依赖当前分析"
        
        return "；".join(advice_parts)
    
    # ============ 知识衰减（V2版-基于实盘验证） ============
    
    def apply_decay(self, decay_days=15, decay_factor=0.95):
        """
        V2版知识衰减：基于实盘验证结果衰减，而非时间衰减
        - 信号的成功率只在新的实盘验证后才调整
        - 时间不再导致衰减（因为信号的成功率是基于历史统计的）
        - 但如果连续多次新数据未验证信号，则降低置信度
        """
        all_knowledge = self.get_patterns("prediction_signal")
        adjusted = 0
        
        for k in all_knowledge:
            k = dict(k)
            metadata = k.get("metadata", "")
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata) if metadata else {}
                except Exception:
                    metadata = {}
            
            decay_mode = metadata.get("decay_mode", "time")
            
            if decay_mode == "verification":
                # 基于验证衰减：不自动衰减，只在reinforce时调整
                # 但检查last_verified是否过久（超过30天未验证）
                last_verified = k.get("last_verified", "")
                if last_verified:
                    try:
                        last_date = datetime.strptime(last_verified, "%Y-%m-%d")
                        days = (datetime.now() - last_date).days
                        if days > 60:
                            # 超过60天未验证，降低置信度
                            old_rate = k.get("success_rate", 0.5) or 0.5
                            new_rate = old_rate * 0.98
                            new_rate = max(0.1, new_rate)
                            self.db.update_knowledge_score(k["id"], round(new_rate, 3), last_verified)
                            adjusted += 1
                    except Exception:
                        pass
            else:
                # 传统时间衰减（非信号类知识）
                last_seen = k.get("last_seen") or k.get("last_verified")
                if not last_seen:
                    continue
                try:
                    last_date = datetime.strptime(last_seen, "%Y-%m-%d")
                    days = (datetime.now() - last_date).days
                    if days > decay_days:
                        old_rate = k.get("success_rate", 0.5) or 0.5
                        new_rate = old_rate * (decay_factor ** (days // 7))
                        new_rate = max(0.1, new_rate)
                        self.db.update_knowledge_score(k["id"], round(new_rate, 3), last_seen)
                        adjusted += 1
                except Exception:
                    pass
        
        if adjusted > 0:
            logger.info(f"知识衰减V2: {adjusted}条知识被调整")
            self._sync_to_json()
        return adjusted
    
    def reinforce(self, knowledge_id, new_evidence):
        """
        用新证据强化知识
        new_evidence: dict with "success" (bool), "date" (str), "actual_result" (str)
        """
        k = self.db.fetch_one("SELECT * FROM market_knowledge WHERE id = ?", (knowledge_id,))
        if not k:
            return
        
        k = dict(k)
        old_rate = k.get("success_rate", 0.5) or 0.5
        occurrence = k.get("occurrence_count", 1) or 1
        
        if new_evidence.get("success"):
            new_rate = min(1.0, old_rate * 1.03 + 0.01)
        else:
            new_rate = max(0.1, old_rate * 0.97 - 0.01)
        
        self.db.update_knowledge_score(knowledge_id, round(new_rate, 3), new_evidence.get("date", ""))
        self.db.execute(
            "UPDATE market_knowledge SET occurrence_count = ? WHERE id = ?",
            (occurrence + 1, knowledge_id))
        
        self._sync_to_json()
    
    # ============ JSON同步 ============
    
    def _sync_to_json(self):
        """将知识库同步到JSON文件"""
        try:
            all_knowledge = self.get_patterns()
            
            by_type = {}
            for k in all_knowledge:
                pt = k.get("pattern_type", "unknown")
                if pt not in by_type:
                    by_type[pt] = []
                by_type[pt].append({
                    "description": k.get("description", ""),
                    "occurrence_count": k.get("occurrence_count", 0),
                    "success_rate": k.get("success_rate", 0.5),
                    "last_seen": k.get("last_seen", ""),
                    "last_verified": k.get("last_verified", ""),
                })
            
            filepath = os.path.join(self.knowledge_dir, "patterns_v2.json")
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump({
                    "updated_at": datetime.now().isoformat(),
                    "total_patterns": len(all_knowledge),
                    "builtin_signals_count": len(self.BUILTIN_SIGNALS),
                    "by_type": by_type,
                }, f, ensure_ascii=False, indent=2)
        
        except Exception as e:
            logger.error(f"同步知识库V2到JSON失败: {e}")
    
    def export_all(self):
        """导出全部知识"""
        self._sync_to_json()
        return self.get_patterns()
    
    def get_stats(self):
        """获取知识库统计"""
        all_k = self.get_patterns()
        by_type = {}
        for k in all_k:
            pt = k.get("pattern_type", "unknown")
            if pt not in by_type:
                by_type[pt] = {"count": 0, "avg_success": 0, "total_success": 0}
            by_type[pt]["count"] += 1
            by_type[pt]["total_success"] += k.get("success_rate", 0.5)
        
        for pt, info in by_type.items():
            info["avg_success"] = round(info["total_success"] / info["count"], 3) if info["count"] else 0
        
        return {
            "total": len(all_k),
            "builtin_signals": len(self.BUILTIN_SIGNALS),
            "by_type": by_type,
        }
