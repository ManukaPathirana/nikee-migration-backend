from fastapi import APIRouter

from app.api.v1 import admin, applications, auth, company, files, health, users, visa

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(company.router)
api_router.include_router(visa.router)
api_router.include_router(applications.router)
api_router.include_router(files.router)
api_router.include_router(admin.router)
