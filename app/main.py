import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from slowapi.extension import _rate_limit_exceeded_handler

from .config import get_settings
from .db.repo import init_database
from .logging_config import configure_logging
from .models import HealthResponse
from .routers import auth, incidents, integrations, vms
from .routers.auth import limiter
from .services.llm import llm_service
from .services.memory import memory_service
from .services.monitor import monitor_loop
from .services.vm_monitor import vm_sweeper_loop

configure_logging()
settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_database()
    tasks = [
        asyncio.create_task(monitor_loop(), name="commit-monitor"),
        asyncio.create_task(vm_sweeper_loop(), name="vm-sweeper"),
    ]
    yield
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title=settings.app_name, version="2.0.0", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(incidents.router)
app.include_router(integrations.router)
app.include_router(vms.router)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        llm_mode=llm_service.mode,
        memory_mode=memory_service.mode,
    )
