"""D7 压测脚本（locust）。

覆盖三个验收目标（P95 在任务 3 里定义）：
- GET /api/v1/notifications/unread-count   P95 < 50ms
- GET /api/v1/dashboard/overview           P95 < 500ms
- GET /api/v1/annotations（跨项目）         P95 < 300ms

运行（先 `python -m app.seed --demo`，再启动 uvicorn）：
    locust -f locustfile.py --host http://127.0.0.1:8010 \
           --headless -u 20 -r 5 -t 30s --csv=perf

账号通过环境变量覆盖（默认使用 seed 的设计师账号，能验证跨项目隔离）：
    LOCUST_EMAIL=designer@arch.com LOCUST_PASSWORD=Passw0rd123
"""
import os
import random

from locust import HttpUser, between, task

API = "/api/v1"
STATUSES = ["PENDING_CONFIRM", "ISSUED", "MODIFIED", "CLOSED"]
SEVERITIES = ["BLOCKER", "MAJOR", "MINOR", "INFO"]


class ReviewUser(HttpUser):
    """一个"登录后不断刷工作台"的审图/设计用户。"""

    wait_time = between(0.1, 0.5)

    def on_start(self) -> None:
        self.email = os.getenv("LOCUST_EMAIL", "designer@arch.com")
        self.password = os.getenv("LOCUST_PASSWORD", "Passw0rd123")
        self.token = ""
        self.login()

    def login(self) -> None:
        resp = self.client.post(
            f"{API}/auth/login",
            json={"email": self.email, "password": self.password, "remember_me": True},
            name="POST /auth/login",
        )
        if resp.status_code != 200:
            raise RuntimeError(f"login failed: {resp.status_code} {resp.text[:200]}")
        self.token = resp.json()["access_token"]

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _get(self, path: str, name: str, **kwargs):
        resp = self.client.get(path, headers=self.auth, name=name, **kwargs)
        # token 过期时原地续一次，避免压测尾段全是 401（不是被测目标）
        if resp.status_code == 401:
            self.login()
            resp = self.client.get(path, headers=self.auth, name=name, **kwargs)
        return resp

    @task(5)
    def unread_count(self) -> None:
        self._get(f"{API}/notifications/unread-count", "GET /notifications/unread-count")

    @task(2)
    def dashboard(self) -> None:
        self._get(f"{API}/dashboard/overview", "GET /dashboard/overview")

    @task(3)
    def cross_project_annotations(self) -> None:
        params: dict[str, str] = {"page": "1", "page_size": "20"}
        if random.random() < 0.6:
            params["assignee_id"] = "me"
        if random.random() < 0.4:
            params["status"] = random.choice(STATUSES)
        if random.random() < 0.3:
            params["severity"] = random.choice(SEVERITIES)
        self._get(
            f"{API}/annotations",
            "GET /annotations (cross-project)",
            params=params,
        )
