"""
knowledge_base.py - 知识库管理模块
管理市场知识的CRUD、模式匹配、知识衰减和强化
"""
import logging
import json
import os
from datetime import datetime

logger = logging.getLogger(__name__)


class KnowledgeBase:
    """知识库管理器"""

    def __init__(self, db, knowledge_dir=None):
        self.db = db
        self.knowledge_dir = knowledge_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "knowledge")
        os.makedirs(self.knowledge_dir, exist_ok=True)

    # ============ 知识CRUD ============
    def add_pattern(self, pattern_type, description, metadata=None):
        """添加新_pattern/知识"""
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
            # 简单的关键词匹配
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
        返回最相似的历史模式及其建议
        """
        current_features = self._extract_features(analysis_result, pattern_result)
        
        # 搜索相似的历史情境
        similar = self.search_similar("market_state", current_features["keywords"])
        
        # 同时查找周期相关的知识
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
        
        # 生成关键词
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
        
        # 基于周期知识的建议
        for c in cycles:
            rate = c.get("success_rate", 0.5)
            if rate > 0.6:
                advice_parts.append(f"历史经验({c['description']}, 成功率{rate*100:.0f}%)")
        
        # 基于相似历史的建议
        for s in similar[:2]:
            k = s["knowledge"]
            advice_parts.append(f"相似情境: {k['description']}(匹配度{s['score']*100:.0f}%)")
        
        if not advice_parts:
            return "知识库中暂无直接匹配的历史经验，建议依赖当前分析"
        
        return "；".join(advice_parts)

    # ============ 知识衰减与强化 ============
    def apply_decay(self, decay_days=15, decay_factor=0.95):
        """
        对长期未验证的知识应用衰减
        """
        all_knowledge = self.get_patterns()
        decayed = 0
        
        for k in all_knowledge:
            k = dict(k)
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
                    decayed += 1
            except:
                pass
        
        if decayed > 0:
            logger.info(f"知识衰减: {decayed}条知识被衰减")
            self._sync_to_json()
        return decayed

    def reinforce(self, knowledge_id, new_evidence):
        """
        用新证据强化知识
        new_evidence: dict with "success" (bool) and "date" (str)
        """
        k = self.db.fetch_one("SELECT * FROM market_knowledge WHERE id = ?", (knowledge_id,))
        if not k:
            return
        
        k = dict(k)
        old_rate = k.get("success_rate", 0.5) or 0.5
        occurrence = k.get("occurrence_count", 1) or 1
        
        if new_evidence.get("success"):
            # 成功验证：提升成功率
            new_rate = min(1.0, old_rate * 1.05 + 0.02)
        else:
            # 失败验证：降低成功率
            new_rate = max(0.1, old_rate * 0.95 - 0.02)
        
        self.db.update_knowledge_score(knowledge_id, round(new_rate, 3), new_evidence.get("date", ""))
        self.db.execute(
            "UPDATE market_knowledge SET occurrence_count = ? WHERE id = ?",
            (occurrence + 1, knowledge_id))
        
        self._sync_to_json()

    # ============ JSON同步 ============
    def _sync_to_json(self):
        """将知识库同步到JSON文件（便于查看和调试）"""
        try:
            all_knowledge = self.get_patterns()
            
            # 按类型分组
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
            
            filepath = os.path.join(self.knowledge_dir, "patterns.json")
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump({
                    "updated_at": datetime.now().isoformat(),
                    "total_patterns": len(all_knowledge),
                    "by_type": by_type,
                }, f, ensure_ascii=False, indent=2)
            
        except Exception as e:
            logger.error(f"同步知识库到JSON失败: {e}")

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
            "by_type": by_type,
        }
