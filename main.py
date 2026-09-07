from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from domain.user import user_router
from process import router as file_router
from db import Base, engine, SessionLocal
from domain.user import user_crud

Base.metadata.create_all(bind=engine)

app = FastAPI()

app.include_router(user_router.router)
app.include_router(file_router)

app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")


@app.on_event("startup")
def startup():
    db = SessionLocal()
    try:
        user_crud.create_admin_if_not_exists(db)
        user_crud.check_dormant_accounts(db)
        user_crud.clean_old_trash(db)
    finally:
        db.close()
