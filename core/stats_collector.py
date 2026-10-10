"""
Dayflow - 统计数据收集器
用于 Web Dashboard 导出功能
"""
import logging
from datetime import date, datetime, timedelta
from typing import List, Dict, Optional

from database.storage import StorageManager

logger = logging.getLogger(__name__)

# 分类颜色映射
CATEGORY_COLORS = {
    "工作": "#7c3aed",    # 紫色
    "学习": "#3b82f6",    # 蓝色
    "编程": "#10b981",    # 绿色
    "会议": "#f59e0b",    # 橙色
    "娱乐": "#ef4444",    # 红色
    "社交": "#ec4899",    # 粉色
    "休息": "#6b7280",    # 灰色
    "其他": "#8b5cf6",    # 浅紫色
}


class StatsCollector:
    """统计数据收集器"""
    
    def __init__(self, storage: StorageManager):
        self.storage = storage
    
    def _get_cards_in_range(self, start_date: date, end_date: date) -> List:
        """获取日期范围内的所有卡片"""
        cards = []
        current = start_date
        while current <= end_date:
            dt = datetime(current.year, current.month, current.day)
            day_cards = self.storage.get_cards_for_date(dt)
            cards.extend(day_cards)
            current += timedelta(days=1)
        return cards
    
    def get_total_duration(self, start_date: date, end_date: date) -> int:
        """
        获取总时长（分钟）
        
        Args:
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            总时长（分钟）
        """
        cards = self._get_cards_in_range(start_date, end_date)
        total_minutes = sum(card.duration_minutes for card in cards)
        return int(total_minutes)
    
    def get_avg_productivity(self, start_date: date, end_date: date) -> float:
        """
        获取平均效率评分
        
        Args:
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            平均效率评分 (0-100)
        """
        cards = self._get_cards_in_range(start_date, end_date)
        if not cards:
            return 0.0
        
        # 按时长加权平均
        total_weighted_score = 0.0
        total_duration = 0.0
        
        for card in cards:
            duration = card.duration_minutes
            if duration > 0:
                total_weighted_score += card.productivity_score * duration
                total_duration += duration
        
        if total_duration == 0:
            return 0.0
        
        return round(total_weighted_score / total_duration, 1)
    
    def get_deep_work_duration(self, start_date: date, end_date: date) -> int:
        """
        获取深度工作时长（效率>=80%的活动）
        
        Args:
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            深度工作时长（分钟）
        """
        cards = self._get_cards_in_range(start_date, end_date)
        deep_work_minutes = sum(
            card.duration_minutes 
            for card in cards 
            if card.productivity_score >= 80
        )
        return int(deep_work_minutes)
    
    def get_activity_count(self, start_date: date, end_date: date) -> int:
        """
        获取活动数量
        
        Args:
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            活动数量
        """
        cards = self._get_cards_in_range(start_date, end_date)
        return len(cards)

    def get_category_distribution(self, start_date: date, end_date: date) -> List[Dict]:
        """
        获取分类时间分布（用于饼图）
        
        Args:
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            分类分布列表 [{name, value, color}]
        """
        cards = self._get_cards_in_range(start_date, end_date)
        
        # 按分类汇总时长
        category_duration = {}
        for card in cards:
            category = card.category or "其他"
            duration = card.duration_minutes
            category_duration[category] = category_duration.get(category, 0) + duration
        
        # 转换为图表数据格式
        result = []
        for category, duration in sorted(category_duration.items(), key=lambda x: -x[1]):
            result.append({
                "name": category,
                "value": round(duration, 1),
                "color": CATEGORY_COLORS.get(category, CATEGORY_COLORS["其他"])
            })
        
        return result
    
    def get_hourly_efficiency(self, target_date: date) -> List[Dict]:
        """
        获取每小时效率数据（用于折线图）
        
        Args:
            target_date: 目标日期
            
        Returns:
            每小时效率列表 [{hour, score, duration}]
        """
        dt = datetime(target_date.year, target_date.month, target_date.day)
        cards = self.storage.get_cards_for_date(dt)
        
        # 初始化 24 小时数据
        hourly_data = {h: {"score_sum": 0, "duration": 0} for h in range(24)}
        
        for card in cards:
            if not card.start_time or not card.end_time:
                continue

            start = card.start_time.replace(tzinfo=None) if card.start_time.tzinfo else card.start_time
            end = card.end_time.replace(tzinfo=None) if card.end_time.tzinfo else card.end_time
            wall_seconds = max((end - start).total_seconds(), 0)
            duration = card.duration_minutes
            if wall_seconds <= 0 or duration <= 0:
                continue
            if wall_seconds > 24 * 3600:
                hourly_data[start.hour]["score_sum"] += card.productivity_score * duration
                hourly_data[start.hour]["duration"] += duration
                continue

            # Split the measured active duration proportionally across hour boundaries.
            cursor = start
            while cursor < end:
                next_hour = cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                segment_end = min(end, next_hour)
                segment_minutes = duration * (segment_end - cursor).total_seconds() / wall_seconds
                hourly_data[cursor.hour]["score_sum"] += card.productivity_score * segment_minutes
                hourly_data[cursor.hour]["duration"] += segment_minutes
                cursor = segment_end
        
        # 计算每小时平均效率
        result = []
        for hour in range(24):
            data = hourly_data[hour]
            if data["duration"] > 0:
                avg_score = data["score_sum"] / data["duration"]
            else:
                avg_score = 0
            
            result.append({
                "hour": hour,
                "score": round(avg_score, 1),
                "duration": round(data["duration"], 1)
            })
        
        return result
    
    def get_weekly_trend(self, end_date: date) -> List[Dict]:
        """
        获取最近7天趋势数据（用于柱状图）
        
        Args:
            end_date: 结束日期
            
        Returns:
            每日趋势列表 [{date, duration, score}]
        """
        result = []
        
        for i in range(6, -1, -1):  # 从7天前到今天
            target_date = end_date - timedelta(days=i)
            dt = datetime(target_date.year, target_date.month, target_date.day)
            cards = self.storage.get_cards_for_date(dt)
            
            total_duration = sum(card.duration_minutes for card in cards)
            
            # 计算加权平均效率
            if total_duration > 0:
                weighted_score = sum(
                    card.productivity_score * card.duration_minutes 
                    for card in cards
                )
                avg_score = weighted_score / total_duration
            else:
                avg_score = 0
            
            result.append({
                "date": target_date.strftime("%m-%d"),
                "weekday": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][target_date.weekday()],
                "duration": round(total_duration, 1),
                "score": round(avg_score, 1)
            })
        
        return result
    
    def get_top_applications(self, start_date: date, end_date: date, limit: int = 5) -> List[Dict]:
        """
        获取使用最多的应用
        
        Args:
            start_date: 开始日期
            end_date: 结束日期
            limit: 返回数量限制
            
        Returns:
            应用列表 [{name, duration, percentage}]
        """
        cards = self._get_cards_in_range(start_date, end_date)
        
        # 汇总应用使用时长
        app_duration = {}
        for card in cards:
            for app_site in card.app_sites:
                name = app_site.name
                duration = app_site.duration_seconds / 60  # 转为分钟
                app_duration[name] = app_duration.get(name, 0) + duration
        
        # 计算总时长
        total_duration = sum(app_duration.values())
        
        # 排序并取 Top N
        sorted_apps = sorted(app_duration.items(), key=lambda x: -x[1])[:limit]
        
        result = []
        for name, duration in sorted_apps:
            percentage = (duration / total_duration * 100) if total_duration > 0 else 0
            result.append({
                "name": name,
                "duration": round(duration, 1),
                "percentage": round(percentage, 1)
            })
        
        return result
    
    def get_activities(self, start_date: date, end_date: date) -> List[Dict]:
        """
        获取活动列表
        
        Args:
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            活动列表 [{start, end, category, title, summary, score, apps}]
        """
        cards = self._get_cards_in_range(start_date, end_date)
        
        # 按开始时间排序
        cards.sort(key=lambda c: c.start_time or datetime.min)
        
        result = []
        for card in cards:
            # 获取主要应用名称
            main_app = card.app_sites[0].name if card.app_sites else ""
            
            result.append({
                "start": card.start_time.strftime("%H:%M") if card.start_time else "",
                "end": card.end_time.strftime("%H:%M") if card.end_time else "",
                "date": card.start_time.strftime("%Y-%m-%d") if card.start_time else "",
                "category": card.category or "其他",
                "category_color": CATEGORY_COLORS.get(card.category, CATEGORY_COLORS["其他"]),
                "title": card.title,
                "summary": card.summary,
                "score": card.productivity_score,
                "duration": round(card.duration_minutes, 1),
                "main_app": main_app,
                "apps": [app.name for app in card.app_sites]
            })
        
        return result
