from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session
from starlette import status
from pydantic import BaseModel
from datetime import datetime, timedelta
from db import get_db
from models import Users, Files, SharedLinks
from auth import get_current_user, SECRET_KEY, ALGORITHM
from jose import jwt, JWTError
import os
import shutil
import uuid
import mimetypes
from dotenv import load_dotenv
from typing import Optional
from user_crud import get_storage_used

load_dotenv()

UPLOAD_DIR = os.getenv("UPLOAD_DIR")
os.makedirs(UPLOAD_DIR, exist_ok=True)

PREVIEWABLE = {
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/svg+xml",
    "application/pdf", "text/plain",
    "video/mp4", "video/webm", "video/ogg", "video/quicktime"
}

router = APIRouter(prefix="/api/file")


class FolderCreate(BaseModel):
    folder_name: str
    parent_id: Optional[int] = None


class ShareCreate(BaseModel):
    file_id: int
    expire_hours: int = 24


class FileMoveBody(BaseModel):
    file_id: int
    new_parent_id: Optional[int] = None


class FileRename(BaseModel):
    new_name: str


def _get_owned_file(db: Session, file_id: int, user: Users):
    f = db.query(Files).filter(
        Files.id == file_id,
        Files.file_owner == user.id,
        Files.is_deleted == False
    ).first()
    if not f:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found.")
    return f


@router.get("/stats")
def get_stats(
    db: Session = Depends(get_db),
    current_user: Users = Depends(get_current_user)
):
    all_files = db.query(Files).filter(
        Files.file_owner == current_user.id,
        Files.is_deleted == False,
        Files.is_folder == False
    ).all()
    total_size = sum(f.file_size or 0 for f in all_files)
    return {"total_files": len(all_files), "total_size": total_size}


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_file(
    file: UploadFile = File(...),
    parent_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: Users = Depends(get_current_user)
):
    if parent_id:
        folder = db.query(Files).filter(
            Files.id == parent_id,
            Files.file_owner == current_user.id,
            Files.is_folder == True,
            Files.is_deleted == False
        ).first()
        if not folder:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found.")
        save_dir = folder.file_path
    else:
        save_dir = current_user.storage_path

    file_path = os.path.join(save_dir, file.filename)

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    file_size = os.path.getsize(file_path)
    file_type = file.content_type or mimetypes.guess_type(file.filename)[0] or "application/octet-stream"

    db_file = Files(
        file_name=file.filename,
        file_path=file_path,
        file_size=file_size,
        file_owner=current_user.id,
        file_type=file_type,
        is_folder=False,
        parent_id=parent_id,
        create_date=datetime.now()
    )
    db.add(db_file)
    db.commit()
    return {"filename": file.filename, "size": file_size}


@router.patch("/rename/{file_id}")
def rename_file(
    file_id: int,
    body: FileRename,
    db: Session = Depends(get_db),
    current_user: Users = Depends(get_current_user)
):
    if not body.new_name or not body.new_name.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Name cannot be empty.")
    file = _get_owned_file(db, file_id, current_user)
    parent_dir = os.path.dirname(file.file_path)
    new_path = os.path.join(parent_dir, body.new_name)
    if os.path.exists(new_path):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A file or folder with this name already exists.")
    os.rename(file.file_path, new_path)
    file.file_name = body.new_name
    file.file_path = new_path
    db.commit()
    return {"message": "Renamed successfully."}


@router.post("/folder", status_code=status.HTTP_201_CREATED)
def create_folder(
    body: FolderCreate,
    db: Session = Depends(get_db),
    current_user: Users = Depends(get_current_user)
):
    if body.parent_id:
        parent = db.query(Files).filter(
            Files.id == body.parent_id,
            Files.file_owner == current_user.id,
            Files.is_folder == True,
            Files.is_deleted == False
        ).first()
        if not parent:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parent folder not found.")
        folder_path = os.path.join(parent.file_path, body.folder_name)
    else:
        folder_path = os.path.join(current_user.storage_path, body.folder_name)

    if os.path.exists(folder_path):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Folder already exists.")

    os.makedirs(folder_path, exist_ok=True)

    db_folder = Files(
        file_name=body.folder_name,
        file_path=folder_path,
        file_size=0,
        file_owner=current_user.id,
        file_type="folder",
        is_folder=True,
        parent_id=body.parent_id,
        create_date=datetime.now()
    )
    db.add(db_folder)
    db.commit()
    return {"folder_name": body.folder_name, "folder_id": db_folder.id}


@router.get("/list")
def get_file_list(
    parent_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: Users = Depends(get_current_user)
):
    items = db.query(Files).filter(
        Files.file_owner == current_user.id,
        Files.parent_id == parent_id,
        Files.is_deleted == False
    ).all()
    return items


@router.get("/search")
def search_files(
    q: str,
    db: Session = Depends(get_db),
    current_user: Users = Depends(get_current_user)
):
    results = db.query(Files).filter(
        Files.file_owner == current_user.id,
        Files.is_deleted == False,
        Files.file_name.ilike(f"%{q}%")
    ).all()
    return results


@router.get("/download/{file_id}")
def download_file(
    file_id: int,
    db: Session = Depends(get_db),
    current_user: Users = Depends(get_current_user)
):
    file = _get_owned_file(db, file_id, current_user)
    if file.is_folder:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot download a folder.")
    return FileResponse(path=file.file_path, filename=file.file_name)


@router.get("/preview/{file_id}")
def preview_file(
    file_id: int,
    request: Request,
    token: str = Query(None),
    db: Session = Depends(get_db)
):
    username = None

    if token:
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            username = payload.get("sub")
        except JWTError:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token.")
    else:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            try:
                payload = jwt.decode(auth_header[7:], SECRET_KEY, algorithms=[ALGORITHM])
                username = payload.get("sub")
            except JWTError:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token.")

    if not username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")

    current_user = db.query(Users).filter(Users.name == username).first()
    if not current_user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found.")

    file = _get_owned_file(db, file_id, current_user)
    if file.is_folder:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot preview a folder.")
    if file.file_type not in PREVIEWABLE:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="This file type cannot be previewed.")

    return FileResponse(
        path=file.file_path,
        media_type=file.file_type,
        headers={"Accept-Ranges": "bytes"}
    )


@router.patch("/move")
def move_file(
    body: FileMoveBody,
    db: Session = Depends(get_db),
    current_user: Users = Depends(get_current_user)
):
    file = _get_owned_file(db, body.file_id, current_user)

    if body.new_parent_id:
        new_parent = db.query(Files).filter(
            Files.id == body.new_parent_id,
            Files.file_owner == current_user.id,
            Files.is_folder == True,
            Files.is_deleted == False
        ).first()
        if not new_parent:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target folder not found.")
        new_dir = new_parent.file_path
    else:
        new_dir = current_user.storage_path

    new_path = os.path.join(new_dir, file.file_name)
    shutil.move(file.file_path, new_path)
    file.file_path = new_path
    file.parent_id = body.new_parent_id
    db.commit()
    return {"message": "Moved successfully."}


@router.delete("/delete/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_file(
    file_id: int,
    db: Session = Depends(get_db),
    current_user: Users = Depends(get_current_user)
):
    file = _get_owned_file(db, file_id, current_user)

    if file.is_folder:
        children = db.query(Files).filter(
            Files.parent_id == file_id,
            Files.is_deleted == False
        ).all()
        for child in children:
            child.is_deleted = True
        if os.path.exists(file.file_path):
            shutil.rmtree(file.file_path)
    else:
        if os.path.exists(file.file_path):
            os.remove(file.file_path)

    file.is_deleted = True
    db.commit()


@router.post("/share")
def create_share_link(
    body: ShareCreate,
    db: Session = Depends(get_db),
    current_user: Users = Depends(get_current_user)
):
    file = _get_owned_file(db, body.file_id, current_user)
    if file.is_folder:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot share a folder.")

    token = str(uuid.uuid4()).replace("-", "")
    expire_at = datetime.now() + timedelta(hours=body.expire_hours)

    link = SharedLinks(
        token=token,
        file_id=file.id,
        owner_id=current_user.id,
        expire_at=expire_at
    )
    db.add(link)
    db.commit()
    return {"token": token, "expire_at": expire_at.isoformat(), "url": f"/api/file/shared/{token}"}


@router.get("/shared/{token}")
def download_shared_file(token: str, db: Session = Depends(get_db)):
    link = db.query(SharedLinks).filter(SharedLinks.token == token).first()
    if not link:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Link not found.")
    if datetime.now() > link.expire_at:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="This link has expired.")

    file = db.query(Files).filter(
        Files.id == link.file_id,
        Files.is_deleted == False
    ).first()
    if not file:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found.")

    return FileResponse(path=file.file_path, filename=file.file_name)


@router.delete("/delete/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_file(file_id: int, db: Session = Depends(get_db), current_user: Users = Depends(get_current_user)):
    file = _get_owned_file(db, file_id, current_user)
    file.is_deleted = True
    file.deleted_at = datetime.now()
    db.commit()


@router.get("/trash")
def get_trash(db: Session = Depends(get_db), current_user: Users = Depends(get_current_user)):
    return db.query(Files).filter(Files.file_owner == current_user.id, Files.is_deleted == True).all()


@router.patch("/trash/restore/{file_id}")
def restore_file(file_id: int, db: Session = Depends(get_db), current_user: Users = Depends(get_current_user)):
    file = db.query(Files).filter(Files.id == file_id, Files.file_owner == current_user.id, Files.is_deleted == True).first()
    if not file:
        raise HTTPException(status_code=404, detail="File not found in trash.")
    file.is_deleted = False
    file.deleted_at = None
    db.commit()
    return {"message": "Restored."}


@router.delete("/trash/permanent/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
def permanent_delete(file_id: int, db: Session = Depends(get_db), current_user: Users = Depends(get_current_user)):
    file = db.query(Files).filter(Files.id == file_id, Files.file_owner == current_user.id, Files.is_deleted == True).first()
    if not file:
        raise HTTPException(status_code=404, detail="File not found in trash.")
    if file.file_path and os.path.exists(file.file_path):
        os.remove(file.file_path)
    db.delete(file)
    db.commit()


@router.delete("/trash/empty", status_code=status.HTTP_204_NO_CONTENT)
def empty_trash(db: Session = Depends(get_db), current_user: Users = Depends(get_current_user)):
    files = db.query(Files).filter(Files.file_owner == current_user.id, Files.is_deleted == True, Files.is_folder == False).all()
    for f in files:
        if f.file_path and os.path.exists(f.file_path):
            os.remove(f.file_path)
        db.delete(f)
    db.commit()


@router.patch("/star/{file_id}")
def toggle_star(file_id: int, db: Session = Depends(get_db), current_user: Users = Depends(get_current_user)):
    file = _get_owned_file(db, file_id, current_user)
    file.is_starred = not file.is_starred
    db.commit()
    return {"is_starred": file.is_starred}


@router.get("/starred")
def get_starred(db: Session = Depends(get_db), current_user: Users = Depends(get_current_user)):
    return db.query(Files).filter(Files.file_owner == current_user.id, Files.is_starred == True, Files.is_deleted == False).all()


@router.get("/stats")
def get_stats(db: Session = Depends(get_db), current_user: Users = Depends(get_current_user)):
    files = db.query(Files).filter(Files.file_owner == current_user.id, Files.is_deleted == False, Files.is_folder == False).all()
    total_size = sum(f.file_size or 0 for f in files)
    trash_count = db.query(Files).filter(Files.file_owner == current_user.id, Files.is_deleted == True).count()
    return {
        "total_files": len(files),
        "total_size": total_size,
        "storage_limit": current_user.storage_limit,
        "trash_count": trash_count
    }
