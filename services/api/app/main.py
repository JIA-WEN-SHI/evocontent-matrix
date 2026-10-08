from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.error_handling import install_error_handlers
from app.routers import accounts, agents, assistant, audit, browser_bridge, coach, content_workflow, domains, execution, kb, ops, pipeline, scheduler, system, webhooks

settings = get_settings()

app = FastAPI(title="EvoContent API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

app.include_router(domains.router)
app.include_router(agents.router)
app.include_router(webhooks.router)
app.include_router(scheduler.router)
app.include_router(audit.router)
app.include_router(pipeline.router)
app.include_router(system.router)
app.include_router(ops.router)
app.include_router(execution.router)
app.include_router(accounts.router)
app.include_router(assistant.router)
app.include_router(coach.router)
app.include_router(kb.router)
app.include_router(browser_bridge.router)
app.include_router(content_workflow.router)
install_error_handlers(app)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
