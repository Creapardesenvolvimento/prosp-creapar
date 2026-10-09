from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.api import auth, numeros, painel, webhooks
from app.api.deps import NotAuthenticated
from app.config import get_settings
from app.log import setup_logging
from app.security import session_secret


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging()

    app = FastAPI(title="Prospecção Creapar", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret(),
        session_cookie="creapar_session",
        max_age=60 * 60 * 24 * 14,
        same_site="lax",
        https_only=settings.is_production,
    )
    app.mount("/static", StaticFiles(directory="app/static"), name="static")

    @app.exception_handler(NotAuthenticated)
    def not_authenticated(request: Request, exc: NotAuthenticated):
        if request.headers.get("HX-Request"):
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login", status_code=303)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    app.include_router(auth.router)
    app.include_router(painel.router)
    app.include_router(numeros.router)
    app.include_router(webhooks.router)
    return app


app = create_app()
