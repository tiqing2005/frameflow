# FrameFlow AI implementation contract

FrameFlow AI is a Chinese-first subtitle-to-visual matching workbench for the recruitment practical assignment. It must demonstrate a real, persistent business loop rather than a static mock.

## Product loop

`text/audio/video input -> durable async job -> transcript -> semantic segments -> >=3 explainable asset candidates per segment -> manual edit/reorder/selection -> refresh-safe saved result`

## Required backend endpoints

All endpoints are under `/api/v1`; errors use `{code,message,retryable,request_id,details?}`.

- `GET /health/live`, `GET /health/ready`
- `GET /dashboard`
- `GET /projects`
- `POST /projects/text` JSON `{title,text}` with optional `Idempotency-Key` header
- `POST /projects/upload` multipart fields `title`, `file`, optional idempotency header
- `GET /projects/{project_id}` returning project, current job, segments, recommendations/selections and trace summary
- `DELETE /projects/{project_id}`
- `GET /jobs/{job_id}` returning job and events
- `POST /jobs/{job_id}/retry`
- `POST /jobs/{job_id}/cancel`
- `PATCH /segments/{segment_id}` JSON `{text?, topic?, keywords?, version}`
- `PUT /projects/{project_id}/segments/order` JSON `{segment_ids}`
- `POST /segments/{segment_id}/rematch`
- `PUT /segments/{segment_id}/selection` JSON `{asset_id}`
- `GET /assets?q=&kind=&tag=`
- `POST /assets` multipart fields `file`, `name`, `tags`, `keywords`
- `PATCH /assets/{asset_id}`
- `GET /runs`
- `GET /audit?project_id=`
- `POST /demo/faults/next` JSON `{mode: "ai_degrade"|"job_fail"|"none"}`

Create calls return the persisted resource and job with HTTP 202. Text processing must be asynchronous and visibly pass through multiple persisted stages. The worker must recover queued/expired jobs after restart. Duplicate idempotency keys return the existing project/job.

## Domain states

- Project: `queued | processing | ready | failed | canceled`
- Job: `queued | running | succeeded | failed | canceled`
- Stages: `validating | extracting | transcribing | segmenting | keywording | matching | persisting | completed`
- Selection source: `auto | manual`

## Matching strategy

Use a transparent hybrid ranker over asset name/tags/keywords:

`0.55 * character n-gram TF-IDF cosine + 0.30 * normalized keyword overlap + 0.15 * tag/topic overlap`.

Return at least three unique candidates. Persist component scores, matched terms, rank, and a Chinese explanation. Mark low-relevance diversity fillers honestly. A configured LLM may enhance segmentation/tagging; rules are the deterministic fallback. Never expose API keys to the browser.

## UX contract

- Chinese UI named `FrameFlow AI` with a professional light editor shell and dark 16:9 preview.
- Main nav: 项目台, 素材库, AI 运行记录, 演示实验室.
- New project supports paste text plus actual audio/video upload UI.
- Processing view shows real server stages/events, progress, failure detail, retry.
- Workbench is three-column on desktop: segment list/raw transcript; preview/editor; >=3 explainable candidates and search replacement.
- All edits show saving/saved feedback and survive refresh.
- Loading skeletons, empty states, toasts, disabled duplicate actions, error banners, responsive mobile tabs.
- Demo fault controls clearly labeled and only for demonstrating fallback/retry behavior.

## Seed data

At least 12 locally served, license-safe image assets covering technology, office/productivity, city/transport, nature/environment, health/sports, food/coffee, education/reading, finance/growth, travel, teamwork, data/security, creativity. Each has Chinese name, tags, and keywords. SVG demo art is acceptable and must be available offline.

## Evidence

- Unit tests for segmentation, keyword extraction, hybrid ranker and idempotency.
- API integration test for create -> job -> result -> edit -> select -> refresh.
- Playwright smoke path if time allows.
- Docker image starts API and worker; SQLite and uploads live under `/data`.
- README includes architecture, setup, deployment, tests, matching rationale, AI-use scope, tradeoffs, known issues and interview demo script.
