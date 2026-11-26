"""菜单定义 + 按权限过滤（prd-05 §4.4：前端不硬编码菜单）。

注意：这是「静态注册表 + 动态过滤」，不是权限定义。权限的唯一权威是 permissions 表。
"""
from __future__ import annotations

from copy import deepcopy

MENU_TREE: list[dict] = [
    {
        "key": "project",
        "title": "项目管理",
        "path": "/projects",
        "icon": "folder",
        "permission": "project:read",
        "children": [],
    },
    {
        "key": "review",
        "title": "预审中心",
        "path": "/review",
        "icon": "scan",
        "permission": "review:read",
        "children": [],
    },
    {
        "key": "annotation",
        "title": "批注协同",
        "path": "/annotations",
        "icon": "edit",
        "permission": "annotation:read",
        "children": [],
    },
    {
        "key": "notification",
        "title": "消息中心",
        "path": "/notifications",
        "icon": "bell",
        "permission": "notification:read",
        "children": [],
    },
    {
        "key": "admin",
        "title": "系统管理",
        "path": "/admin",
        "icon": "setting",
        "permission": "system:user:manage",
        "children": [
            {
                "key": "admin-user",
                "title": "用户管理",
                "path": "/admin/users",
                "icon": "user",
                "permission": "system:user:manage",
                "children": [],
            },
            {
                "key": "admin-role",
                "title": "角色权限",
                "path": "/admin/roles",
                "icon": "safety",
                "permission": "system:role:read",
                "children": [],
            },
            {
                "key": "admin-audit",
                "title": "审计日志",
                "path": "/admin/audit",
                "icon": "file-search",
                "permission": "system:audit:read",
                "children": [],
            },
        ],
    },
]


def build_menus(permissions: set[str], superuser: bool = False) -> list[dict]:
    """按权限码过滤菜单树；父节点无权限则整枝剪掉，子节点同理。"""

    def _allowed(item: dict) -> bool:
        if superuser or "*" in permissions:
            return True
        perm = item.get("permission")
        return perm is None or perm in permissions

    def _walk(items: list[dict]) -> list[dict]:
        result: list[dict] = []
        for raw in items:
            if not _allowed(raw):
                continue
            node = deepcopy(raw)
            node["children"] = _walk(raw.get("children", []))
            result.append(node)
        return result

    return _walk(MENU_TREE)
