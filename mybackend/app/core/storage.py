"""本地文件存储：分片写入 / 流式合并 / 魔数校验 / 路径约束。

设计要点（详见 D3 复盘）：
1. 分片先写 `.{index}.{uuid}.tmp` 再 `os.replace` 成 `{index}.part` —— 读者永远看不到半个文件；
2. 合并用 1MB 分块循环 + 增量哈希，内存占用与文件大小无关；
3. 所有阻塞 IO 都丢到 `asyncio.to_thread`，不阻塞事件循环；
4. 最终路径必须落在 UPLOAD_DIR 内（防目录穿越）。
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from app.core.config import settings
from app.core.errors import AppError

UPLOAD_ROOT = Path(settings.UPLOAD_DIR).resolve()
MERGE_BUF_SIZE = 1 << 20  # 1MB
MAGIC_HEAD_BYTES = 512


# ------------------------------------------------------------------ 路径
def ensure_root() -> Path:
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    (UPLOAD_ROOT / "temp").mkdir(parents=True, exist_ok=True)
    return UPLOAD_ROOT


def temp_dir(upload_id: uuid.UUID | str) -> Path:
    return UPLOAD_ROOT / "temp" / str(upload_id)


def final_path(project_id: uuid.UUID | str, drawing_id: uuid.UUID | str, ext: str) -> Path:
    return UPLOAD_ROOT / str(project_id) / f"{drawing_id}.{ext.lstrip('.')}"


def storage_key(project_id: uuid.UUID | str, drawing_id: uuid.UUID | str, ext: str) -> str:
    """存进 DB 的相对 key（不带 UPLOAD_ROOT 前缀，便于换存储根目录）。"""
    return f"{project_id}/{drawing_id}.{ext.lstrip('.')}"


def resolve_key(key: str) -> Path:
    """把 storage_key 还原成绝对路径，并强制约束在 UPLOAD_ROOT 内。"""
    root = ensure_root()
    p = (root / key).resolve()
    if not p.is_relative_to(root):  # Python 3.9+
        raise AppError("FORBIDDEN", "非法的文件路径", 403)
    return p


# ------------------------------------------------------------------ 分片
async def write_chunk(upload_id: uuid.UUID, index: int, stream: AsyncIterator[bytes]) -> int:
    """把请求体流式落盘为分片；返回写入字节数。

    并发安全：每个分片先写带 uuid 的临时文件，写完再原子 rename。
    即使两个请求同时写同一个 index，也只会有一个最终生效，不会出现"半个文件"。
    """
    d = temp_dir(upload_id)
    d.mkdir(parents=True, exist_ok=True)
    final = d / f"{index:05d}.part"
    tmp = d / f".{index:05d}.{uuid.uuid4().hex}.tmp"

    total = 0
    fh = await asyncio.to_thread(open, tmp, "wb")
    try:
        async for block in stream:
            if not block:
                continue
            total += len(block)
            if total > settings.CHUNK_SIZE:
                raise AppError(
                    "ERR_CHUNK_TOO_LARGE",
                    f"分片大小超过上限 {settings.CHUNK_SIZE} 字节",
                    422,
                    {"index": index, "max_bytes": settings.CHUNK_SIZE},
                )
            await asyncio.to_thread(fh.write, block)
    except BaseException:
        await asyncio.to_thread(fh.close)
        await asyncio.to_thread(_safe_unlink, tmp)
        raise
    await asyncio.to_thread(fh.close)

    # 原子替换：读者要么看到旧分片，要么看到完整新分片
    await asyncio.to_thread(os.replace, tmp, final)
    return total


def list_chunks(upload_id: uuid.UUID) -> set[int]:
    d = temp_dir(upload_id)
    if not d.exists():
        return set()
    out: set[int] = set()
    for p in d.glob("*.part"):
        try:
            out.add(int(p.stem))
        except ValueError:
            continue
    return out


def count_chunks(upload_id: uuid.UUID) -> int:
    return len(list_chunks(upload_id))


# ------------------------------------------------------------------ 合并
def merge_chunks(upload_id: uuid.UUID, chunk_total: int, dest: Path) -> tuple[int, str, str]:
    """流式合并分片并边读边算 SHA256/MD5；返回 (size, sha256, md5)。

    内存占用恒定（1MB 缓冲），与文件大小无关 —— 100MB PDF 不会把 100MB 读进内存。
    """
    tmp = dest.with_name(dest.name + ".merging")
    tmp.parent.mkdir(parents=True, exist_ok=True)

    sha = hashlib.sha256()
    md5 = hashlib.md5()
    size = 0

    try:
        with open(tmp, "wb") as out:
            for i in range(chunk_total):
                part = temp_dir(upload_id) / f"{i:05d}.part"
                if not part.exists():
                    raise AppError(
                        "ERR_CHUNK_MISSING",
                        "分片不完整，无法合并",
                        422,
                        {"missing_index": i},
                    )
                with open(part, "rb") as f:
                    while True:
                        block = f.read(MERGE_BUF_SIZE)
                        if not block:
                            break
                        out.write(block)
                        sha.update(block)
                        md5.update(block)
                        size += len(block)
        # 合并完成后一次性原子落位，避免前端拿到"合并中"的半成品
        os.replace(tmp, dest)
    except BaseException:
        _safe_unlink(tmp)
        raise

    return size, sha.hexdigest(), md5.hexdigest()


def sha256_file(path: Path, buf: int = MERGE_BUF_SIZE) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(buf)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


# ------------------------------------------------------------------ 校验
def detect_file_type(path: Path) -> str | None:
    """魔数识别：PDF 以 %PDF 开头；DXF 前 512 字节内含 SECTION。

    不看后缀 —— 后缀是用户给的，魔数是文件自己说的。
    """
    with open(path, "rb") as f:
        head = f.read(MAGIC_HEAD_BYTES)
    if head[:4] == b"%PDF":
        return "pdf"
    if b"SECTION" in head:
        return "dxf"
    return None


def sniff_filename_suffix(file_name: str) -> str:
    return Path(file_name).suffix.lower().lstrip(".")


# ------------------------------------------------------------------ 清理
def remove_chunk_dir(upload_id: uuid.UUID) -> None:
    """尽力清理临时目录：失败只记录，不影响主流程（复盘 2.4）。"""
    d = temp_dir(upload_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


def _safe_unlink(p: Path) -> None:
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass
