from __future__ import annotations

import html
import json
from pathlib import Path

from sqlalchemy.orm import Session

from .config import Settings
from .models import Asset, FaultControl


SEED_ASSETS = [
    ("technology", "智能科技", ["科技", "人工智能", "未来"], ["AI", "算法", "芯片", "创新", "数字化"], "#5B5BD6", "#9B8AFB"),
    ("office", "高效办公", ["办公", "效率", "计划"], ["工作", "时间管理", "生产力", "会议", "电脑"], "#2563EB", "#38BDF8"),
    ("city", "城市交通", ["城市", "交通", "出行"], ["地铁", "汽车", "道路", "通勤", "现代城市"], "#0F766E", "#2DD4BF"),
    ("nature", "绿色自然", ["自然", "环境", "可持续"], ["森林", "树木", "生态", "环保", "绿色"], "#15803D", "#84CC16"),
    ("health", "健康运动", ["健康", "运动", "活力"], ["跑步", "健身", "身体", "生活方式", "训练"], "#DC2626", "#FB7185"),
    ("coffee", "咖啡时光", ["美食", "咖啡", "生活"], ["饮品", "早餐", "休息", "餐饮", "温暖"], "#92400E", "#F59E0B"),
    ("education", "阅读学习", ["教育", "阅读", "成长"], ["书籍", "知识", "学习", "课堂", "学生"], "#7C3AED", "#C084FC"),
    ("finance", "金融增长", ["金融", "商业", "增长"], ["投资", "数据", "趋势", "收益", "经济"], "#0369A1", "#22D3EE"),
    ("travel", "旅行探索", ["旅行", "探索", "风景"], ["远方", "地图", "假期", "目的地", "冒险"], "#C2410C", "#FDBA74"),
    ("teamwork", "团队协作", ["团队", "协作", "沟通"], ["伙伴", "合作", "会议", "组织", "共同目标"], "#BE185D", "#F472B6"),
    ("security", "数据安全", ["数据", "安全", "隐私"], ["网络安全", "保护", "密码", "云计算", "风险"], "#334155", "#60A5FA"),
    ("creativity", "创意灵感", ["创意", "设计", "艺术"], ["灵感", "想法", "视觉", "创造力", "内容"], "#A21CAF", "#F0ABFC"),
]


def _svg(name: str, primary: str, accent: str, index: int) -> str:
    escaped = html.escape(name)
    x = 180 + (index % 4) * 70
    y = 150 + (index % 3) * 55
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720" role="img" aria-label="{escaped}">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop stop-color="{primary}"/><stop offset="1" stop-color="{accent}"/></linearGradient><filter id="b"><feGaussianBlur stdDeviation="42"/></filter></defs>
<rect width="1280" height="720" rx="36" fill="#0B1020"/><circle cx="1040" cy="120" r="250" fill="{accent}" opacity=".22" filter="url(#b)"/>
<circle cx="{x}" cy="{y}" r="150" fill="url(#g)" opacity=".92"/><rect x="110" y="420" width="1060" height="180" rx="36" fill="#FFFFFF" opacity=".1"/>
<path d="M160 510 C300 {350 + index*8}, 440 610, 610 470 S930 350,1120 500" fill="none" stroke="{accent}" stroke-width="18" stroke-linecap="round" opacity=".85"/>
<text x="120" y="655" fill="#F8FAFC" font-family="Arial,'Microsoft YaHei',sans-serif" font-weight="700" font-size="58">{escaped}</text>
<text x="1160" y="658" fill="#CBD5E1" text-anchor="end" font-family="Arial,sans-serif" font-size="24">FRAMEFLOW / DEMO ASSET</text>
</svg>"""


def seed_assets(session: Session, settings: Settings) -> None:
    seed_dir = settings.data_dir / "media" / "seed"
    seed_dir.mkdir(parents=True, exist_ok=True)
    for index, (slug, name, tags, keywords, primary, accent) in enumerate(SEED_ASSETS):
        path = seed_dir / f"{slug}.svg"
        if not path.exists():
            path.write_text(_svg(name, primary, accent, index), encoding="utf-8")
        asset_id = f"seed-{slug}"
        if session.get(Asset, asset_id) is None:
            session.add(
                Asset(
                    id=asset_id,
                    name=name,
                    kind="image",
                    public_url=f"/media/seed/{slug}.svg",
                    storage_path=str(path),
                    mime_type="image/svg+xml",
                    size_bytes=path.stat().st_size,
                    tags_json=json.dumps(tags, ensure_ascii=False),
                    keywords_json=json.dumps(keywords, ensure_ascii=False),
                    is_seed=True,
                    active=True,
                )
            )
    if session.get(FaultControl, 1) is None:
        session.add(FaultControl(id=1, next_mode="none"))

