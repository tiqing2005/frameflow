from __future__ import annotations

from app.models import Job, Project


def test_dashboard_reports_job_states_separately(runtime):
    client, _worker, database, _settings = runtime

    with database.session() as session:
        projects = [
            Project(title=f"仪表盘状态 {index}", status="processing", input_kind="text")
            for index in range(5)
        ]
        session.add_all(projects)
        session.flush()
        session.add_all(
            [
                Job(project_id=projects[0].id, status="queued"),
                Job(project_id=projects[1].id, status="queued"),
                Job(project_id=projects[2].id, status="running"),
                Job(project_id=projects[3].id, status="failed"),
                Job(project_id=projects[4].id, status="succeeded"),
            ]
        )

    response = client.get("/api/v1/dashboard")

    assert response.status_code == 200
    metrics = response.json()["metrics"]
    assert metrics["projects"] == 5
    assert metrics["queued_jobs"] == 2
    assert metrics["running_jobs"] == 1
    assert metrics["failed_jobs"] == 1
