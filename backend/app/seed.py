from __future__ import annotations

import html
import json
import shutil
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .models import Asset, FaultControl


# The six-field tuple shape is intentionally stable: evaluation/evaluate.py
# reads this literal without importing SQLAlchemy.
SEED_ASSETS = [
    ("technology", "AI 芯片实验室", ["科技", "人工智能", "硬件"], ["AI芯片", "服务器", "算力", "实验室", "数字化"], "#30343B", "#E4572E"),
    ("technology-human", "人机协同开发", ["科技", "交互", "未来"], ["工程师", "代码", "自动化", "智能助手", "软件开发"], "#30343B", "#E4572E"),
    ("office", "晨光专注工位", ["办公", "效率", "专注"], ["办公室", "电脑", "晨光", "计划", "生产力"], "#34373D", "#D97736"),
    ("office-meeting", "项目协作空间", ["办公", "项目", "会议"], ["会议室", "排期", "复盘", "团队会议", "工作流"], "#34373D", "#D97736"),
    ("city", "清晨城市通勤", ["城市", "交通", "通勤"], ["城市", "道路", "现代建筑", "早高峰", "公共交通"], "#27333A", "#D95D39"),
    ("city-night", "夕照城市天际线", ["城市", "道路", "出行"], ["城市夜景", "车流", "写字楼", "智慧交通", "城市节奏"], "#27333A", "#D95D39"),
    ("nature", "晨光森林", ["自然", "森林", "生态"], ["树木", "阳光", "氧气", "宁静", "环保"], "#27352D", "#71945B"),
    ("nature-green", "山林与湖泊", ["自然", "环境", "可持续"], ["山脉", "森林", "湖泊", "绿色", "清洁环境"], "#27352D", "#71945B"),
    ("health", "城市晨练", ["健康", "跑步", "活力"], ["运动", "训练", "自律", "健康生活", "体能"], "#3B3030", "#D65A45"),
    ("health-training", "力量训练", ["健康", "健身", "力量"], ["健身房", "杠铃", "汗水", "训练", "突破"], "#3B3030", "#D65A45"),
    ("coffee", "咖啡分享时刻", ["美食", "咖啡", "生活"], ["咖啡", "饮品", "朋友", "休息", "暖光"], "#3B322B", "#B8733D"),
    ("coffee-window", "窗边咖啡馆", ["咖啡", "阅读", "休息"], ["咖啡馆", "窗光", "午后", "放松", "慢生活"], "#3B322B", "#B8733D"),
    ("education", "图书馆阅读", ["教育", "阅读", "学习"], ["书籍", "图书馆", "知识", "课程", "成长"], "#30343A", "#B38654"),
    ("education-online", "知识书架", ["教育", "在线", "成长"], ["书架", "学习", "课程", "阅读", "远程教育"], "#30343A", "#B38654"),
    ("finance", "数据分析工作台", ["金融", "数据", "商业"], ["走势图", "分析", "投资", "趋势", "数据决策"], "#29343A", "#4D8A76"),
    ("finance-growth", "商业增长分析", ["金融", "商业", "增长"], ["电脑", "仪表盘", "市场", "经济", "收益"], "#29343A", "#4D8A76"),
    ("travel", "山谷公路远行", ["旅行", "探索", "公路"], ["公路", "山脉", "远方", "自驾", "冒险"], "#3A3129", "#C97945"),
    ("travel-coast", "山海探索", ["旅行", "户外", "假期"], ["海岸", "山峰", "背包", "自由", "目的地"], "#3A3129", "#C97945"),
    ("teamwork", "团队共创会议", ["团队", "协作", "沟通"], ["团队", "讨论", "电脑", "共同目标", "合作"], "#30343A", "#C35D49"),
    ("teamwork-success", "开放式团队空间", ["团队", "成果", "伙伴"], ["办公室", "伙伴", "里程碑", "成功", "企业文化"], "#30343A", "#C35D49"),
    ("security", "数字支付安全", ["数据", "安全", "云计算"], ["支付", "账户", "网络", "防护", "基础设施"], "#282D33", "#527A86"),
    ("security-auth", "芯片与身份认证", ["安全", "隐私", "认证"], ["芯片", "双重验证", "密码", "账户保护", "网络安全"], "#282D33", "#527A86"),
    ("creativity", "设计师色彩实验", ["创意", "设计", "视觉"], ["色彩", "画笔", "排版", "灵感", "创作"], "#383035", "#C05D58"),
    ("creativity-craft", "抽象艺术创作", ["创意", "艺术", "手作"], ["颜料", "工作室", "工艺", "艺术", "创造力"], "#383035", "#C05D58"),
]

SEED_VIDEOS = [
    ("video-smart-city", "数字城市脉动", ["科技", "城市", "数据"], ["城市夜景", "航拍", "交通网络", "智慧城市", "车流"]),
    ("video-focus-work", "专注工作节奏", ["办公", "效率", "专注"], ["工作流", "工位", "时间管理", "办公室", "生产力"]),
    ("video-forest-breath", "森林呼吸", ["自然", "生态", "疗愈"], ["森林", "阳光", "树叶", "微风", "环保"]),
    ("video-running", "奔跑与突破", ["运动", "跑步", "活力"], ["跑者", "训练", "健康", "自律", "晨练"]),
    ("video-team-collab", "协作到成果", ["团队", "创意", "成果"], ["讨论", "合作", "项目", "伙伴", "共同目标"]),
    ("video-travel-road", "山海远行", ["旅行", "公路", "自然"], ["公路", "山脉", "远方", "自由", "探索"]),
]


def _fallback_svg(name: str, primary: str, accent: str) -> str:
    escaped = html.escape(name)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720">
<rect width="1280" height="720" fill="{primary}"/><rect x="80" y="80" width="1120" height="560" rx="18" fill="none" stroke="{accent}" stroke-width="4"/>
<text x="110" y="610" fill="#fff" font-family="Arial,'Microsoft YaHei',sans-serif" font-weight="700" font-size="58">{escaped}</text>
</svg>"""


def _copy_media(source: Path, target: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"seed media missing: {source}")
    if not target.exists() or target.stat().st_size != source.stat().st_size:
        shutil.copy2(source, target)


def _upsert_asset(
    session: Session,
    *,
    slug: str,
    name: str,
    kind: str,
    path: Path,
    public_url: str,
    mime_type: str,
    tags: list[str],
    keywords: list[str],
) -> str:
    asset_id = f"seed-{slug}"
    asset = session.get(Asset, asset_id)
    if asset is None:
        asset = Asset(id=asset_id)
        session.add(asset)
    asset.name = name
    asset.kind = kind
    asset.public_url = public_url
    asset.storage_path = str(path)
    asset.mime_type = mime_type
    asset.size_bytes = path.stat().st_size
    asset.tags_json = json.dumps(tags, ensure_ascii=False)
    asset.keywords_json = json.dumps(keywords, ensure_ascii=False)
    asset.is_seed = True
    asset.active = True
    return asset_id


def seed_assets(session: Session, settings: Settings) -> None:
    seed_dir = settings.data_dir / "media" / "seed"
    seed_dir.mkdir(parents=True, exist_ok=True)
    source_dir = Path(__file__).resolve().parents[1] / "seed_media"
    active_ids: set[str] = set()

    for slug, name, tags, keywords, primary, accent in SEED_ASSETS:
        source = source_dir / f"{slug}.jpg"
        target = seed_dir / f"{slug}.jpg"
        if source.is_file():
            _copy_media(source, target)
            mime = "image/jpeg"
        else:
            target = seed_dir / f"{slug}.svg"
            target.write_text(_fallback_svg(name, primary, accent), encoding="utf-8")
            mime = "image/svg+xml"
        active_ids.add(
            _upsert_asset(
                session,
                slug=slug,
                name=name,
                kind="image",
                path=target,
                public_url=f"/media/seed/{target.name}",
                mime_type=mime,
                tags=tags,
                keywords=keywords,
            )
        )

    for slug, name, tags, keywords in SEED_VIDEOS:
        source = source_dir / f"{slug}.mp4"
        target = seed_dir / f"{slug}.mp4"
        _copy_media(source, target)
        active_ids.add(
            _upsert_asset(
                session,
                slug=slug,
                name=name,
                kind="video",
                path=target,
                public_url=f"/media/seed/{target.name}",
                mime_type="video/mp4",
                tags=tags,
                keywords=keywords,
            )
        )

    for asset in session.scalars(select(Asset).where(Asset.is_seed.is_(True))).all():
        if asset.id not in active_ids:
            asset.active = False

    if session.get(FaultControl, 1) is None:
        session.add(FaultControl(id=1, next_mode="none"))
