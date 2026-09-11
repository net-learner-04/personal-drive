from fastapi import APIRouter, HTTPException, Depends, UploadFile, File
from sqlalchemy.orm import Session
from starlette import status
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.responses import FileResponse
from config import UPLOAD_DIR, PROFILE_DIR
import os
import shutil
from db import get_db
from models import Users
from auth import create_access_token, get_current_user
from mailer import send_reset_code, send_register_code, verify_code, send_dormant_unlock_code
from domain.user import user_crud, user_schema

os.makedirs(PROFILE_DIR, exist_ok=True)

router = APIRouter(prefix="/api/user")


def require_admin(current_user: Users = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required.")
    return current_user


@router.post("/email/verify-request")
def email_verify_request(body: user_schema.EmailVerifyRequest, db: Session = Depends(get_db)):
    existing = db.query(Users).filter(Users.email == body.email).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already in use.")
    send_register_code(body.email)
    return {"message": "Verification code sent."}


@router.post("/email/verify-confirm")
def email_verify_confirm(body: user_schema.EmailVerifyConfirm):
    if not verify_code(body.email, body.code, code_type="register"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired code.")
    return {"message": "Email verified."}


@router.post("/create", status_code=status.HTTP_204_NO_CONTENT)
def user_create(_user_create: user_schema.UserCreate, db: Session = Depends(get_db)):
    user = user_crud.get_existing_user(db, user_create=_user_create)
    if user:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This user already exists.")
    result = user_crud.create_user(db=db, user_create=_user_create)
    if not result:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Maximum number of members ({user_crud.MAX_ACCOUNT}) has been exceeded.")


@router.post("/login", response_model=user_schema.Token)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user, error, dormant_user = user_crud.check_login_by_email(db, form_data.username, form_data.password)
    if dormant_user:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="dormant")
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=error)
    access_token = create_access_token(data={"sub": user.name})
    return {"access_token": access_token, "token_type": "bearer"}


@router.get("/me", response_model=user_schema.UserResponse)
def get_me(current_user: Users = Depends(get_current_user)):
    return current_user


@router.post("/settings/verify")
def verify_password_for_settings(
    body: user_schema.UserVerifyPassword,
    current_user: Users = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if not user_crud.verify_password(db, current_user, body.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect password.")
    return {"message": "Password verified."}


@router.patch("/settings/name")
def update_name(
    body: user_schema.UserUpdateName,
    current_user: Users = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if not user_crud.verify_password(db, current_user, body.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect password.")
    existing = db.query(Users).filter(Users.name == body.new_name).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already taken.")
    user_crud.update_name(db, current_user, body.new_name)
    new_token = create_access_token(data={"sub": body.new_name})
    return {"message": "Username updated.", "access_token": new_token}


@router.patch("/settings/email")
def update_email(
    body: user_schema.UserUpdateEmail,
    current_user: Users = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if not user_crud.verify_password(db, current_user, body.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect password.")
    existing = db.query(Users).filter(Users.email == body.new_email).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already in use.")
    user_crud.update_email(db, current_user, body.new_email)
    return {"message": "Email updated."}


@router.patch("/settings/password")
def update_password(
    body: user_schema.UserUpdatePassword,
    current_user: Users = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if not user_crud.verify_password(db, current_user, body.current_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Current password is incorrect.")
    user_crud.update_password(db, current_user, body.new_password)
    return {"message": "Password updated."}


@router.post("/settings/profile-image")
async def upload_profile_image(
    file: UploadFile = File(...),
    current_user: Users = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    allowed_types = {"image/jpeg", "image/png", "image/webp", "image/gif"}
    if file.content_type not in allowed_types:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="Only image files are allowed.")

    ext = file.filename.rsplit(".", 1)[-1].lower()
    filename = f"{current_user.id}_profile.{ext}"
    file_path = os.path.join(PROFILE_DIR, filename)

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    user_crud.update_profile_image(db, current_user, file_path)
    return {"message": "Profile image updated.", "path": f"/api/user/profile-image/{current_user.id}"}


@router.get("/profile-image/{user_id}")
def get_profile_image(user_id: int, db: Session = Depends(get_db)):
    user = user_crud.get_user_by_id(db, user_id)
    if not user or not user.profile_image or not os.path.exists(user.profile_image):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile image not found.")
    return FileResponse(user.profile_image)


@router.delete("/settings/delete", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(
    body: user_schema.UserDelete,
    current_user: Users = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin account cannot be deleted this way.")
    if not user_crud.verify_password(db, current_user, body.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect password.")
    user_crud.delete_user(db, current_user)


@router.post("/password/reset-request")
def reset_request(email: str, db: Session = Depends(get_db)):
    user = db.query(Users).filter(Users.email == email).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This email address does not exist.")
    send_reset_code(email)
    return {"message": "A verification code has been sent."}


@router.post("/password/reset")
def reset_password(email: str, code: str, new_password: str, db: Session = Depends(get_db)):
    if not verify_code(email, code, code_type="reset"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The verification code is incorrect or has expired.")
    user = db.query(Users).filter(Users.email == email).first()
    user_crud.update_password(db, user, new_password)
    return {"message": "Your password has been changed."}


@router.post("/dormant/unlock-request")
def dormant_unlock_request(email: str, db: Session = Depends(get_db)):
    user = db.query(Users).filter(Users.email == email).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Email not found.")
    if not user.is_dormant:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Account is not dormant.")
    send_dormant_unlock_code(email)
    return {"message": "Unlock code sent to your email."}


@router.post("/dormant/unlock-confirm")
def dormant_unlock_confirm(email: str, code: str, db: Session = Depends(get_db)):
    if not verify_code(email, code, code_type="dormant"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired code.")
    user = db.query(Users).filter(Users.email == email).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    user_crud.activate_dormant(db, user)
    return {"message": "Account reactivated. You can now sign in."}


@router.get("/admin/users", response_model=list[user_schema.UserResponse])
def admin_get_users(
    db: Session = Depends(get_db),
    current_user: Users = Depends(require_admin)
):
    return user_crud.get_all_users(db)


@router.delete("/admin/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def admin_delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: Users = Depends(require_admin)
):
    user = user_crud.get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    if user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot delete admin account.")
    user_crud.delete_user(db, user)


@router.patch("/admin/users/{user_id}/lock", status_code=status.HTTP_204_NO_CONTENT)
def admin_lock_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: Users = Depends(require_admin)
):
    user = user_crud.get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    if user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot lock admin account.")
    user_crud.lock_user(db, user)


@router.patch("/admin/users/{user_id}/unlock", status_code=status.HTTP_204_NO_CONTENT)
def admin_unlock_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: Users = Depends(require_admin)
):
    user = user_crud.get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    user_crud.unlock_user(db, user)


@router.patch("/admin/users/{user_id}/dormant-unlock", status_code=status.HTTP_204_NO_CONTENT)
def admin_dormant_unlock(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: Users = Depends(require_admin)
):
    user = user_crud.get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    user_crud.activate_dormant(db, user)


@router.get("/admin/config")
def get_config(current_user: Users = Depends(require_admin)):
    return {"max_account": user_crud.MAX_ACCOUNT}


@router.delete("/settings/profile-image", status_code=status.HTTP_204_NO_CONTENT)
def delete_profile_image(
    current_user: Users = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if current_user.profile_image and os.path.exists(current_user.profile_image):
        os.remove(current_user.profile_image)
    user_crud.update_profile_image(db, current_user, None)


@router.patch("/admin/users/{user_id}/storage-limit")
def admin_set_storage_limit(
    user_id: int,
    limit_gb: float = None,
    db: Session = Depends(get_db),
    current_user: Users = Depends(require_admin)
):
    user = user_crud.get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    limit_bytes = int(limit_gb * 1073741824) if limit_gb is not None else None
    user_crud.set_storage_limit(db, user, limit_bytes)
    return {"message": "Storage limit updated."}
