import re
from pathlib import Path

import yaml
from fastapi import FastAPI, Request, Depends
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from src.core.models import Council, Application, ScrapeRun
from src.dashboard.dependencies import get_db

CONFIG_DIR = Path(__file__).parent.parent / "config" / "councils"


def _get_disabled_councils():
    """Load disabled council info from YAML configs."""
    disabled = []
    for f in sorted(CONFIG_DIR.glob("*.yml")):
        content = f.read_text()
        data = yaml.safe_load(content)
        if data.get("enabled") is False:
            reason_match = re.search(r"# disabled_reason: (.+)", content)
            if reason_match:
                reason = reason_match.group(1)
            else:
                comment_match = re.search(r"^# (.+)$", content, re.MULTILINE)
                reason = comment_match.group(1) if comment_match else "No reason specified"
            status = data.get("status", "")
            disabled.append({
                "authority_code": data["authority_code"],
                "name": data.get("name", data["authority_code"]),
                "platform": data.get("platform", ""),
                "reason": reason,
                "archived": data.get("archived", False),
                "status": status,
            })
    return disabled

TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app():
    app = FastAPI(title="UK Planning Dashboard")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    def render(request, name, context):
        context["request"] = request
        return templates.TemplateResponse(request, name, context)

    @app.get("/")
    async def index():
        return RedirectResponse(url="/search")

    @app.get("/search")
    async def search(
        request: Request,
        q: str = "",
        council: str = "",
        page: int = 1,
        db: Session = Depends(get_db),
    ):
        per_page = 50
        offset = (page - 1) * per_page
        filters = []
        if q:
            filters.append(
                Application.description.ilike(f"%{q}%")
                | Application.address.ilike(f"%{q}%")
            )
        if council:
            filters.append(Council.authority_code == council)

        count_query = select(func.count(Application.id)).join(Council)
        list_query = select(Application).join(Council)
        for f in filters:
            count_query = count_query.where(f)
            list_query = list_query.where(f)

        total = db.execute(count_query).scalar()
        applications = db.execute(
            list_query.order_by(Application.first_scraped_at.desc()).offset(offset).limit(per_page)
        ).scalars().all()
        councils = db.execute(select(Council).order_by(Council.name)).scalars().all()
        return render(request, "search.html", {
            "applications": applications, "councils": councils,
            "q": q, "selected_council": council, "page": page,
            "total": total, "per_page": per_page,
        })

    @app.get("/councils")
    async def councils_list(request: Request, db: Session = Depends(get_db)):
        councils = db.execute(select(Council).order_by(Council.name)).scalars().all()
        stats = {}
        for c in councils:
            app_count = db.execute(select(func.count(Application.id)).where(Application.council_id == c.id)).scalar()
            last_run = db.execute(
                select(ScrapeRun).where(ScrapeRun.council_id == c.id).order_by(ScrapeRun.id.desc()).limit(1)
            ).scalar_one_or_none()
            stats[c.id] = {"app_count": app_count, "last_run": last_run}
        disabled_councils = _get_disabled_councils()
        disabled_reasons = {c["authority_code"]: c["reason"] for c in disabled_councils}
        return render(request, "councils.html", {
            "councils": councils, "stats": stats,
            "disabled_councils": disabled_councils,
            "disabled_reasons": disabled_reasons,
        })

    @app.get("/councils/{authority_code}")
    async def council_detail(request: Request, authority_code: str, page: int = 1, db: Session = Depends(get_db)):
        council = db.execute(select(Council).where(Council.authority_code == authority_code)).scalar_one_or_none()
        if not council:
            return render(request, "council.html", {
                "council": None, "applications": [], "runs": [],
                "page": 1, "total": 0, "per_page": 50,
            })
        per_page = 50
        offset = (page - 1) * per_page
        total = db.execute(select(func.count(Application.id)).where(Application.council_id == council.id)).scalar()
        applications = db.execute(
            select(Application).where(Application.council_id == council.id)
            .order_by(Application.first_scraped_at.desc()).offset(offset).limit(per_page)
        ).scalars().all()
        runs = db.execute(
            select(ScrapeRun).where(ScrapeRun.council_id == council.id).order_by(ScrapeRun.id.desc()).limit(20)
        ).scalars().all()
        return render(request, "council.html", {
            "council": council, "applications": applications,
            "runs": runs, "page": page, "total": total, "per_page": per_page,
        })

    @app.get("/applications/{app_id}")
    async def application_detail(request: Request, app_id: int, db: Session = Depends(get_db)):
        application = db.execute(select(Application).where(Application.id == app_id)).scalar_one_or_none()
        council = None
        if application:
            council = db.execute(select(Council).where(Council.id == application.council_id)).scalar_one_or_none()
        return render(request, "application.html", {
            "application": application, "council": council,
        })

    return app
