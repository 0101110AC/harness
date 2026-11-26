"""初始化种子数据（幂等，可重复执行）。

用法：
    python -m app.seed                  # 角色/权限/三个账号/三个项目（无图纸）
    python -m app.seed --create-tables  # 开发期无迁移时直接建表（不推荐用于生产）
    python -m app.seed --demo           # 额外写入图纸、预审、批注（演示/压测基线）

D7 演示基线（任务 4）：
    用户：admin（管理员）/ reviewer（审图专员）/ designer（设计师）
    项目：滨江办公楼、云栖科技园、城南小学改扩建（空项目，演示空态）
    图纸：前两个项目各 2 张（PDF + DXF）
    批注：PENDING_CONFIRM / ISSUED / MODIFIED / CLOSED 全覆盖；
          另含 AI 来源且本周/上周关闭的批注，供工作台「确认数 / 环比 / 拦截率」有数可看
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import uuid as _uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.core.security import hash_password
from app.database import AsyncSessionLocal, engine
from app.models.annotation import Annotation, AnnotationStatusLog
from app.models.base import Base
from app.models.drawing import Drawing
from app.models.enums import (
    AnnotationSource,
    AnnotationStatus,
    Discipline,
    DrawingStatus,
    FileType,
    IssueCategory,
    IssueStatus,
    LogType,
    NotificationChannel,
    ProjectRole,
    ProjectStatus,
    ReviewMode,
    ReviewStage,
    ReviewTaskStatus,
    RoleCode,
    RunStatus,
    Severity,
)
from app.models.notification import NotificationPreference
from app.models.project import Project, ProjectMember
from app.models.review import ReviewIssue, ReviewTask
from app.models.system import AuditLog
from app.models.user import Permission, Role, User

log = logging.getLogger(__name__)

# ---- 权限码：prd-05 §4.2（16 条，唯一权威）----
PERMISSIONS: list[tuple[str, str, str]] = [
    ("project:read", "查看项目", "project"),
    ("project:write", "创建/编辑项目", "project"),
    ("project:member:manage", "管理项目成员", "project"),
    ("drawing:read", "查看图纸", "drawing"),
    ("drawing:upload", "上传图纸", "drawing"),
    ("drawing:delete", "删除图纸", "drawing"),
    ("review:read", "查看预审结果", "review"),
    ("review:execute", "发起/重跑预审", "review"),
    ("annotation:read", "查看批注", "annotation"),
    ("annotation:write", "创建/编辑批注", "annotation"),
    ("annotation:transition", "批注状态流转", "annotation"),
    ("annotation:delete", "删除批注", "annotation"),
    ("notification:read", "查看站内信", "notification"),
    ("system:user:manage", "用户管理", "system"),
    ("system:role:read", "角色权限查看", "system"),
    ("system:audit:read", "审计日志", "system"),
]

ALL_PERMS = [code for code, _, _ in PERMISSIONS]

ROLES: list[tuple[RoleCode, str, str]] = [
    (RoleCode.ADMIN, "管理员", "系统管理与全部业务权限"),
    (RoleCode.REVIEWER, "审图专员", "上传图纸、执行预审、维护批注"),
    (RoleCode.DESIGNER, "设计师", "接收批注、上传新版本"),
    (RoleCode.VIEWER, "只读访客", "仅查看（预留）"),
]

REVIEWER_PERMS = [
    "project:read",
    "project:write",
    "project:member:manage",
    "drawing:read",
    "drawing:upload",
    "drawing:delete",
    "review:read",
    "review:execute",
    "annotation:read",
    "annotation:write",
    "annotation:transition",
    "annotation:delete",
    "notification:read",
]
DESIGNER_PERMS = [
    "project:read",
    "drawing:read",
    "drawing:upload",
    "review:read",
    "annotation:read",
    "annotation:transition",
    "notification:read",
]
VIEWER_PERMS = [
    "project:read",
    "drawing:read",
    "review:read",
    "annotation:read",
    "notification:read",
]

ROLE_PERMISSION_MAP: dict[RoleCode, list[str]] = {
    RoleCode.ADMIN: ALL_PERMS,
    RoleCode.REVIEWER: REVIEWER_PERMS,
    RoleCode.DESIGNER: DESIGNER_PERMS,
    RoleCode.VIEWER: VIEWER_PERMS,
}

DEMO_PASSWORD = "Passw0rd123"
OPEN_STATUSES = (
    AnnotationStatus.PENDING_CONFIRM,
    AnnotationStatus.ISSUED,
    AnnotationStatus.MODIFIED,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _week_start(now: datetime) -> datetime:
    return (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


async def _upsert_roles(db: AsyncSession) -> dict[RoleCode, Role]:
    result: dict[RoleCode, Role] = {}
    for code, name, desc in ROLES:
        role = (await db.execute(select(Role).where(Role.code == code))).scalar_one_or_none()
        if role is None:
            role = Role(code=code, name=name, description=desc, is_system=True)
            db.add(role)
            await db.flush()
            log.info("role created: %s", code.value)
        else:
            role.name, role.description, role.is_system = name, desc, True
        result[code] = role
    return result


async def _upsert_permissions(db: AsyncSession) -> dict[str, Permission]:
    result: dict[str, Permission] = {}
    for code, name, module in PERMISSIONS:
        perm = (
            await db.execute(select(Permission).where(Permission.code == code))
        ).scalar_one_or_none()
        if perm is None:
            perm = Permission(code=code, name=name, module=module)
            db.add(perm)
            await db.flush()
        else:
            perm.name, perm.module = name, module
        result[code] = perm
    return result


async def _bind_permissions(
    db: AsyncSession, roles: dict[RoleCode, Role], perms: dict[str, Permission]
) -> None:
    """用关联对象显式绑定；已存在则跳过（幂等）。"""
    from app.models.user import RolePermission

    for role_code, codes in ROLE_PERMISSION_MAP.items():
        role = roles[role_code]
        existing = {
            rp.permission_id
            for rp in (
                await db.execute(
                    select(RolePermission).where(RolePermission.role_id == role.id)
                )
            ).scalars()
        }
        for code in codes:
            perm = perms[code]
            if perm.id in existing:
                continue
            db.add(RolePermission(role_id=role.id, permission_id=perm.id))
    await db.flush()


async def _upsert_user(
    db: AsyncSession, *, name: str, email: str, role: Role
) -> User:
    email = email.lower()
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        user = User(name=name, email=email, password_hash=hash_password(DEMO_PASSWORD))
        user.roles = [role]
        db.add(user)
        await db.flush()
        log.info("user created: %s", email)
    else:
        if not any(r.code == role.code for r in user.roles):
            user.roles.append(role)

    pref = (
        await db.execute(
            select(NotificationPreference).where(
                NotificationPreference.user_id == user.id,
                NotificationPreference.channel == NotificationChannel.INAPP,
            )
        )
    ).scalar_one_or_none()
    if pref is None:
        db.add(
            NotificationPreference(
                user_id=user.id,
                channel=NotificationChannel.INAPP,
                enabled=True,
                event_types=[],
            )
        )
    return user


# ------------------------------------------------------------------ 项目
async def _upsert_project(
    db: AsyncSession,
    *,
    name: str,
    code: str,
    description: str,
    building_type: str,
    design_stage: str,
    owner: User,
    members: list[tuple[User, ProjectRole]],
) -> Project:
    project = (
        await db.execute(select(Project).where(Project.code == code))
    ).scalar_one_or_none()
    if project is None:
        project = Project(
            name=name,
            code=code,
            description=description,
            building_type=building_type,
            design_stage=design_stage,
            status=ProjectStatus.ACTIVE,
            owner_id=owner.id,
        )
        db.add(project)
        await db.flush()
        log.info("project created: %s", name)
    else:
        project.name = name
        project.description = description

    for user, prole in members:
        member = (
            await db.execute(
                select(ProjectMember).where(
                    ProjectMember.project_id == project.id,
                    ProjectMember.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if member is None:
            db.add(
                ProjectMember(project_id=project.id, user_id=user.id, project_role=prole)
            )
        else:
            member.project_role = prole
    return project


# ------------------------------------------------------------------ 文件
def _minimal_pdf(text: str) -> bytes:
    """生成一个合法（带 xref）的极简单页 PDF，A1 幅面，供前端真实预览。"""
    import io as _io

    body_text = f"BT /F1 36 Tf 100 1500 Td ({text}) Tj ET".encode("ascii", "ignore")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 2384 1684] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(body_text)).encode() + b" >>\nstream\n" + body_text + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = _io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    xref_at = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode()
    )
    return out.getvalue()


def _minimal_dxf() -> bytes:
    """生成合法 ASCII DXF：HEADER + WALL/DOOR 图层直线 + EOF。"""
    lines = [
        "999", "ARCH-HARNESS DEMO DXF", "0", "SECTION", "2", "HEADER",
        "9", "$ACADVER", "1", "AC1015",
        "9", "$INSUNITS", "70", "4",
        "0", "ENDSEC", "0", "SECTION", "2", "ENTITIES",
        "0", "LINE", "8", "WALL", "10", "0.0", "20", "0.0", "11", "84000.0", "21", "59400.0",
        "0", "LINE", "8", "DOOR", "10", "0.0", "20", "0.0", "11", "10.0", "21", "10.0",
        "0", "ENDSEC", "0", "EOF",
    ]
    return ("\n".join(lines) + "\n").encode("ascii")


def _write_demo_file(project_id, drawing_id, ftype, title: str) -> tuple[str, str, int]:
    """把示例文件写到正式路径，返回 (storage_key, sha256, size)。"""
    dest = storage.final_path(project_id, drawing_id, ftype.value)
    if not dest.exists() or dest.stat().st_size == 0:
        dest.parent.mkdir(parents=True, exist_ok=True)
        payload = _minimal_pdf(title) if ftype is FileType.PDF else _minimal_dxf()
        dest.write_bytes(payload)
    return (
        storage.storage_key(project_id, drawing_id, ftype.value),
        storage.sha256_file(dest),
        dest.stat().st_size,
    )


async def _ensure_drawings(
    db: AsyncSession,
    project: Project,
    uploader: User,
    specs: list[tuple[str, str, FileType, str]],
) -> list[Drawing]:
    created: list[Drawing] = []
    for name, fname, ftype, title in specs:
        exists = (
            await db.execute(
                select(Drawing).where(
                    Drawing.project_id == project.id, Drawing.name == name
                )
            )
        ).scalar_one_or_none()

        drawing_id = exists.id if exists is not None else _uuid.uuid4()
        key, checksum, size = _write_demo_file(project.id, drawing_id, ftype, title)

        if exists is not None:
            # 兼容 D2 时期写入的假 key：把真实文件与校验值回填
            exists.storage_key = key
            exists.checksum = checksum
            exists.file_size = size
            exists.page_count = 1
            created.append(exists)
            continue

        d = Drawing(
            id=drawing_id,
            project_id=project.id,
            name=name,
            file_name=fname,
            file_type=ftype,
            file_size=size,
            storage_key=key,
            checksum=checksum,
            version=1,
            is_current=True,
            status=DrawingStatus.REVIEW_DONE,
            page_count=1,
            parse_meta={"page_count": 1, "pages": [{"w": 2384, "h": 1684}]},
            uploaded_by=uploader.id,
        )
        db.add(d)
        created.append(d)
    await db.flush()
    project.drawing_count = len(created)
    return created


async def _ensure_review_demo(
    db: AsyncSession, project: Project, drawing: Drawing, user: User
) -> None:
    """一张完成的预审任务 + 6 条问题（问题页/报告页有数据可看）。"""
    task = (
        await db.execute(select(ReviewTask).where(ReviewTask.drawing_id == drawing.id))
    ).scalar_one_or_none()
    if task is not None:
        return

    task = ReviewTask(
        project_id=project.id,
        drawing_id=drawing.id,
        status=ReviewTaskStatus.SUCCEEDED,
        stage=ReviewStage.DONE,
        stage_text="预审完成",
        progress=100,
        engine="mock",
        issue_total=6,
        issue_blocker=2,
        summary={"by_severity": {"BLOCKER": 2, "MAJOR": 2, "MINOR": 2, "INFO": 0}},
        thread_id=f"demo-thread-{drawing.id}",
        run_id=f"demo-run-{drawing.id}",
        run_status=RunStatus.SUCCEEDED,
        review_mode=ReviewMode.PROFESSIONAL,
        created_by=user.id,
        cost_ms=6200,
    )
    db.add(task)
    await db.flush()

    samples = [
        (Severity.BLOCKER, Discipline.ARCH, IssueCategory.CODE, "3 层疏散楼梯净宽不足 1.1m", "GB50016-2014-5.5.18", 92),
        (Severity.BLOCKER, Discipline.ARCH, IssueCategory.CODE, "防火分区面积超过规范限值", "GB50016-2014-5.3.1", 88),
        (Severity.MAJOR, Discipline.MEP, IssueCategory.CLASH, "强电井与给水立管碰撞", "CLASH-MEP-001", 76),
        (Severity.MAJOR, Discipline.ARCH, IssueCategory.CONSISTENCY, "门窗开启方向与立面图不符", "CONSIST-ELEV-002", 71),
        (Severity.MINOR, Discipline.ARCH, IssueCategory.COMPLETENESS, "图签缺少设计阶段标注", "COMPLETE-TITLEBLOCK", 85),
        (Severity.MINOR, Discipline.GENERAL, IssueCategory.CONSISTENCY, "轴线编号在 4~5 轴重复", "CONSIST-AXIS", 64),
    ]
    for i, (sev, disc, cat, title, rule, conf) in enumerate(samples):
        db.add(
            ReviewIssue(
                review_task_id=task.id,
                drawing_id=drawing.id,
                project_id=project.id,
                category=cat,
                severity=sev,
                discipline=disc,
                title=title,
                description=f"依据 {rule} 判定，需人工复核。",
                suggestion="按规范调整后重新提交。",
                rule_code=rule,
                confidence=conf,
                location={
                    "file_type": "pdf",
                    "page_no": 1,
                    "bbox": {"x": 120.0 + i * 40, "y": 200.0 + i * 30, "w": 180.0, "h": 90.0},
                    "unit": "pt",
                    "coord_system": "pdf_user_space",
                    "origin": "top-left",
                    "page_size": {"w": 2384, "h": 1684},
                    "dxf": None,
                },
                location_valid=True,
                status=IssueStatus.PENDING,
            )
        )


# ------------------------------------------------------------------ 批注
def _location(drawing: Drawing, index: int) -> dict:
    is_pdf = drawing.file_type is FileType.PDF
    return {
        "file_type": drawing.file_type.value,
        "page_no": 1,
        "bbox": {
            "x": 120.0 + index * 55,
            "y": 200.0 + index * 45,
            "w": 180.0,
            "h": 90.0,
        },
        "unit": "pt" if is_pdf else "mm",
        "coord_system": "pdf_user_space" if is_pdf else "dxf_wcs",
        "origin": "top-left" if is_pdf else "bottom-left",
        "page_size": {"w": 2384, "h": 1684} if is_pdf else {"w": 84000, "h": 59400},
        "dxf": None if is_pdf else {"extents": {"x": 0.0, "y": 0.0, "w": 84000.0, "h": 59400.0}},
    }


async def _ensure_annotation(
    db: AsyncSession,
    *,
    project: Project,
    drawing: Drawing,
    index: int,
    title: str,
    status: AnnotationStatus,
    severity: Severity,
    creator: User,
    assignee: User,
    source: AnnotationSource = AnnotationSource.HUMAN,
    content: str | None = None,
    closed_at: datetime | None = None,
    close_remark: str | None = None,
    created_at: datetime | None = None,
) -> Annotation:
    exists = (
        await db.execute(
            select(Annotation).where(
                Annotation.project_id == project.id, Annotation.title == title
            )
        )
    ).scalar_one_or_none()
    if exists is not None:
        return exists

    now = _now()
    ann = Annotation(
        project_id=project.id,
        drawing_id=drawing.id,
        drawing_version=drawing.version,
        source=source,
        shape_type="RECT",
        location=_location(drawing, index),
        location_valid=True,
        title=title,
        content=content or f"演示批注：{title}，请按规范复核。",
        severity=severity,
        discipline=Discipline.ARCH,
        status=status,
        assignee_id=assignee.id,
        creator_id=creator.id,
        closed_at=closed_at,
        close_remark=close_remark if closed_at else None,
        row_version=1,
        comment_count=0,
        created_at=created_at or now,
    )
    db.add(ann)
    await db.flush()

    db.add(
        AnnotationStatusLog(
            annotation_id=ann.id,
            log_type=LogType.CREATED,
            to_status=AnnotationStatus.PENDING_CONFIRM,
            operator_id=creator.id,
            created_at=now - timedelta(days=3),
        )
    )
    if status is not AnnotationStatus.PENDING_CONFIRM:
        db.add(
            AnnotationStatusLog(
                annotation_id=ann.id,
                log_type=LogType.STATUS_CHANGED,
                from_status=AnnotationStatus.PENDING_CONFIRM,
                to_status=status,
                operator_id=creator.id,
                remark="演示数据：批量推进状态",
                created_at=now - timedelta(days=2),
            )
        )
    if status is AnnotationStatus.CLOSED and closed_at is not None:
        db.add(
            AnnotationStatusLog(
                annotation_id=ann.id,
                log_type=LogType.CLOSED,
                from_status=AnnotationStatus.MODIFIED,
                to_status=AnnotationStatus.CLOSED,
                operator_id=creator.id,
                remark=close_remark,
                created_at=closed_at,
            )
        )
    return ann


async def _ensure_demo_annotations(
    db: AsyncSession,
    project: Project,
    drawings: list[Drawing],
    reviewer: User,
    designer: User,
) -> None:
    """四种状态全覆盖 + 本周/上周 AI 关闭，供列表、看板、工作台演示。"""
    now = _now()
    this_start = _week_start(now)
    # 取本周已经过去的一半位置：无论今天是周一 00:05 还是周日，都保证落在 [周一, 现在)
    this_week = this_start + (now - this_start) / 2
    last_week = this_start - timedelta(days=2)
    pdf = drawings[0]
    dxf = drawings[1]

    items = [
        # 阶段一：待确认 / 已下发 / 已修改 / 已关闭（人工）
        dict(drawing=pdf, index=0, title="首层疏散宽度不足", status=AnnotationStatus.PENDING_CONFIRM,
             severity=Severity.BLOCKER),
        dict(drawing=pdf, index=1, title="防火分区面积超限", status=AnnotationStatus.ISSUED,
             severity=Severity.BLOCKER),
        dict(drawing=dxf, index=2, title="门窗开启方向不一致", status=AnnotationStatus.MODIFIED,
             severity=Severity.MAJOR),
        dict(drawing=dxf, index=3, title="图签缺少设计阶段", status=AnnotationStatus.CLOSED,
             severity=Severity.MINOR, closed_at=now - timedelta(days=2), close_remark="已补充图签"),
        # 阶段二：AI 来源，本周关闭（工作台「本周确认数」）
        dict(drawing=pdf, index=4, title="容积率超出规划条件", status=AnnotationStatus.CLOSED,
             severity=Severity.MAJOR, source=AnnotationSource.AI, closed_at=this_week,
             close_remark="设计已调整"),
    ]
    # AI 来源的"本周输出总数"给拦截率一个分母
    items.append(
        dict(drawing=pdf, index=5, title="绿地率未达规划要求", status=AnnotationStatus.ISSUED,
             severity=Severity.MINOR, source=AnnotationSource.AI)
    )
    # AI 来源，上周关闭（工作台环比）；created_at 也落在上周之前，
    # 否则会被算进"本周 AI 输出总数"，把拦截率分母抬高。
    items.append(
        dict(drawing=dxf, index=6, title="消防车道转弯半径不足", status=AnnotationStatus.CLOSED,
             severity=Severity.MAJOR, source=AnnotationSource.AI, closed_at=last_week,
             close_remark="已调整车道", created_at=last_week - timedelta(days=3))
    )

    for i, item in enumerate(items):
        drawing = item.pop("drawing")
        index = item.pop("index", i)
        await _ensure_annotation(
            db,
            project=project,
            drawing=drawing,
            index=index,
            creator=reviewer,
            assignee=designer,
            **item,
        )


async def _recount_project(db: AsyncSession, project: Project) -> None:
    drawings = (
        await db.execute(
            select(func.count()).select_from(Drawing).where(
                Drawing.project_id == project.id, Drawing.is_deleted.is_(False)
            )
        )
    ).scalar_one()
    open_count = (
        await db.execute(
            select(func.count()).select_from(Annotation).where(
                Annotation.project_id == project.id,
                Annotation.is_deleted.is_(False),
                Annotation.status.in_(OPEN_STATUSES),
            )
        )
    ).scalar_one()
    project.drawing_count = drawings
    project.open_annotation_count = open_count


async def _ensure_audit_demo(db: AsyncSession, actor: User, users: list[User]) -> None:
    """审计页要有内容可看：为种子账号补一条"建号"历史（幂等）。

    真实管理操作由 audit_service 在业务事务里写入；这里只是演示基线。
    IP 固定 127.0.0.1，避免 seed 在没有请求上下文时记成空值。
    """
    for user in users:
        exists = (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.action == "USER_CREATE", AuditLog.target_id == user.id
                )
            )
        ).first()
        if exists is not None:
            continue
        db.add(
            AuditLog(
                user_id=actor.id,
                action="USER_CREATE",
                target_type="USER",
                target_id=user.id,
                target_name=user.name,
                detail={"seed": True, "email": user.email, "roles": [r.code.value for r in user.roles]},
                ip="127.0.0.1",
            )
        )


async def seed(with_demo: bool = False) -> None:
    async with AsyncSessionLocal() as db:
        try:
            roles = await _upsert_roles(db)
            perms = await _upsert_permissions(db)
            await _bind_permissions(db, roles, perms)
            await db.flush()

            admin = await _upsert_user(
                db, name="管理员", email="admin@arch.com", role=roles[RoleCode.ADMIN]
            )
            reviewer = await _upsert_user(
                db, name="张工", email="reviewer@arch.com", role=roles[RoleCode.REVIEWER]
            )
            designer = await _upsert_user(
                db, name="李工", email="designer@arch.com", role=roles[RoleCode.DESIGNER]
            )

            members = [
                (admin, ProjectRole.OWNER),
                (reviewer, ProjectRole.REVIEWER),
                (designer, ProjectRole.DESIGNER),
            ]
            riverside = await _upsert_project(
                db,
                name="滨江办公楼",
                code="ARCH-DEMO-001",
                description="演示项目：12 层办公楼，含 PDF 与 DXF 图纸各一",
                building_type="办公",
                design_stage="SCHEME",
                owner=admin,
                members=members,
            )
            yunqi = await _upsert_project(
                db,
                name="云栖科技园",
                code="ARCH-DEMO-002",
                description="演示项目：产业园总图与立面，批注状态全覆盖",
                building_type="产业园",
                design_stage="CONSTRUCTION",
                owner=admin,
                members=members,
            )
            empty = await _upsert_project(
                db,
                name="城南小学改扩建",
                code="ARCH-DEMO-003",
                description="演示项目：空项目（无图纸 / 无批注），用于空态页面",
                building_type="教育",
                design_stage="SCHEME",
                owner=admin,
                members=[(admin, ProjectRole.OWNER), (designer, ProjectRole.VIEWER)],
            )

            if with_demo:
                riverside_drawings = await _ensure_drawings(
                    db,
                    riverside,
                    admin,
                    [
                        ("首层平面图", "首层平面图.pdf", FileType.PDF, "DEMO DRAWING A1"),
                        ("标准层平面图", "标准层平面图.dxf", FileType.DXF, ""),
                    ],
                )
                yunqi_drawings = await _ensure_drawings(
                    db,
                    yunqi,
                    reviewer,
                    [
                        ("总平面图", "总平面图.pdf", FileType.PDF, "DEMO YUNQI MASTER PLAN"),
                        ("南立面图", "南立面图.dxf", FileType.DXF, ""),
                    ],
                )
                await _ensure_review_demo(db, riverside, riverside_drawings[0], admin)
                # 工作台演示：云栖的两张图纸停在"待审"和"预审中"，
                # 否则所有图纸都是 REVIEW_DONE，pending_drawings 永远为 0。
                yunqi_drawings[0].status = DrawingStatus.UPLOADED
                yunqi_drawings[1].status = DrawingStatus.REVIEWING
                await _ensure_demo_annotations(
                    db, riverside, riverside_drawings, reviewer, designer
                )
                await _ensure_demo_annotations(
                    db, yunqi, yunqi_drawings, reviewer, designer
                )

            await _ensure_audit_demo(db, admin, [admin, reviewer, designer])
            for project in (riverside, yunqi, empty):
                await _recount_project(db, project)

            await db.commit()
            log.info("seed done.")
            print("=" * 64)
            print("seed 完成：")
            print("  角色 4 / 权限 16 / 绑定 ADMIN 16、REVIEWER 13、DESIGNER 7、VIEWER 5")
            print(f"  账号 admin@arch.com    / {DEMO_PASSWORD}  (ADMIN)")
            print(f"  账号 reviewer@arch.com / {DEMO_PASSWORD}  (REVIEWER)")
            print(f"  账号 designer@arch.com / {DEMO_PASSWORD}  (DESIGNER)")
            print("  项目 滨江办公楼 / 云栖科技园（含图纸与批注）")
            print("       城南小学改扩建（空项目，演示空态）")
            if with_demo:
                print("  图纸 2+2（PDF + DXF）/ 批注覆盖 待确认·已下发·已修改·已关闭")
                print("  工作台：本周/上周均有 AI 关闭记录，环比与拦截率可展示")
            print("=" * 64)
        except Exception:
            await db.rollback()
            raise


async def main() -> None:
    parser = argparse.ArgumentParser(description="Arch-Harness 种子数据")
    parser.add_argument(
        "--create-tables",
        action="store_true",
        help="开发期直接按 ORM 建表（生产请用 alembic upgrade head）",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="写入演示图纸 / 预审 / 批注（D7 演示与压测基线）",
    )
    args = parser.parse_args()

    if args.create_tables:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        log.info("tables created (dev only)")

    await seed(with_demo=args.demo)
    await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
    asyncio.run(main())
