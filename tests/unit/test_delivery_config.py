"""阶段 14 交付配置的测试（计划第 996–1010 行）。

验收是「全新环境 clone 后按 README 能一键起全栈」——那需要真的起容器，
属人工/E2E 范畴。这里覆盖能在纯 Python 下验证的部分：

1. **镜像内不含密钥**（计划第 1006 行）——这条件最要紧，且能静态验证；
2. compose 文件的结构正确（5 个服务、迁移独立、端口只绑回环）；
3. README 覆盖了计划要求的几件事（环境变量、启动、最小流程）；
4. CI 只做跑测试这一件事（计划第 1008 行）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# 本文件在 tests/unit/ 下，项目根要上溯三层
PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ============================================================
# 镜像内不含密钥（计划第 1006 行）
# ============================================================


def test_dockerignore_excludes_env_and_credentials():
    """.env 与凭据文件必须被 .dockerignore 挡住。

    这一条比"构建后进去看"更重要：`COPY . .` 一旦把 .env 拷进镜像层，
    即使后续删掉，密钥仍然留在层历史里，照样能被 docker history 捞出来。
    """
    content = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")
    patterns = {
        line.strip()
        for line in content.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    # `.env.*` 加 `!.env.example` 是"挡住 .env 全家但保留模板"的标准写法
    assert ".env" in patterns
    assert ".env.*" in patterns
    assert "!.env.example" in patterns, "模板要保留，否则新环境无从下手"
    assert ".credentials.yaml" in patterns or "**/.credentials.yaml" in patterns


def test_dockerignore_excludes_local_state_and_secrets():
    content = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")
    for pattern in (".venv/", "__pycache__/", "*.pem", "*.key", ".git/"):
        assert pattern in content, f".dockerignore 缺少 {pattern}"


def test_dockerfile_does_not_copy_secrets_or_embed_keys():
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    # 不得有 ENV/ARG 形式的密钥
    assert not re.search(r"^\s*(ENV|ARG)\s+.*(API_KEY|SECRET|PASSWORD)", dockerfile, re.MULTILINE), (
        "Dockerfile 里出现了密钥类的 ENV/ARG —— 那会被写进镜像元数据"
    )
    # 不得硬编码 sk- 开头的串
    assert not re.search(r"sk-[A-Za-z0-9]{16,}", dockerfile), "Dockerfile 里硬编码了密钥"


def test_dockerfile_runs_as_non_root():
    """容器以非 root 运行，减小逃逸影响面。"""
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^\s*USER\s+(?!root)\S+", dockerfile, re.MULTILINE), "应当切换到非 root 用户"


def test_no_real_key_anywhere_in_tracked_delivery_files():
    """交付相关文件里不得出现真实密钥。"""
    real_key_fragment = "REDACTED_REAL_KEY"
    for name in (
        "Dockerfile", ".dockerignore", "docker-compose.yml",
        "README.md", ".env.example", ".github/workflows/ci.yml",
    ):
        path = PROJECT_ROOT / name
        if path.is_file():
            assert real_key_fragment not in path.read_text(encoding="utf-8"), (
                f"{name} 里出现了真实密钥"
            )


# ============================================================
# compose 结构（计划第 996–1003 行）
# ============================================================


@pytest.fixture(scope="module")
def compose_text() -> str:
    return (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")


@pytest.mark.parametrize("service", ["web", "worker", "beat", "postgres", "redis"])
def test_compose_defines_the_five_planned_services(compose_text: str, service: str):
    assert re.search(rf"^\s{{2}}{service}:", compose_text, re.MULTILINE), f"compose 缺少服务 {service}"


def test_compose_has_a_dedicated_migrate_service(compose_text: str):
    """迁移走独立服务并跑完即退出，避免三个容器并发跑同一批 DDL。"""
    assert re.search(r"^\s{2}migrate:", compose_text, re.MULTILINE)
    assert "alembic" in compose_text and "upgrade" in compose_text
    assert 'restart: "no"' in compose_text, "迁移服务不该常驻"


def test_app_services_wait_for_migration(compose_text: str):
    """web/worker/beat 必须等迁移成功后才启动，否则会对着空库跑。"""
    assert compose_text.count("condition: service_completed_successfully") >= 4


def test_ports_are_bound_to_loopback_only(compose_text: str):
    """只绑 127.0.0.1：不暴露到局域网（与只读 + 本地的定位一致）。"""
    published = re.findall(r'-\s*"([^"]+:\d+)"', compose_text)
    assert published, "应当有端口映射"
    for mapping in published:
        assert mapping.startswith("127.0.0.1:"), f"端口 {mapping} 没有只绑回环"


def test_compose_uses_env_file_not_inline_secrets(compose_text: str):
    """密钥从 .env 运行时注入，不写在 compose 里。"""
    assert "env_file:" in compose_text
    assert not re.search(r"sk-[A-Za-z0-9]{16,}", compose_text), "compose 里硬编码了密钥"


def test_worker_uses_solo_pool(compose_text: str):
    """Windows 直跑必须 -P solo；容器内也用同一档，行为一致便于对照排障。"""
    assert "solo" in compose_text


def test_uploads_and_data_share_a_volume(compose_text: str):
    """web 写入的脱敏文件，worker 分析时要能读到 —— 必须共用同一个卷。"""
    assert "appdata:/app/data" in compose_text
    volume_users = compose_text.count("appdata:/app/data")
    assert volume_users >= 3, "web/worker/beat 都应挂载同一个数据卷"


# ============================================================
# README 覆盖计划要求的内容（计划第 1007 行）
# ============================================================


@pytest.fixture(scope="module")
def readme() -> str:
    return (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")


def test_readme_documents_the_one_command_start(readme: str):
    assert "docker compose up -d" in readme


def test_readme_documents_environment_variables(readme: str):
    assert "环境变量" in readme
    for variable in ("MODEL_PROVIDER_API_KEY", "SECRET_KEY", "DATABASE_URL", "MONITORING"[:0] or "MODEL_L1"):
        assert variable in readme, f"README 未说明 {variable}"


def test_readme_documents_the_minimal_usage_flow(readme: str):
    for step in ("上传", "发起分析", "看报告"):
        assert step in readme, f"README 缺少「{step}」这一步"


def test_readme_explains_the_exited_migrate_service(readme: str):
    """`Exited (0)` 最容易被误判成故障，必须写明。"""
    assert "Exited (0)" in readme


def test_readme_states_where_keys_go(readme: str):
    assert "密钥放哪" in readme or "MODEL_PROVIDER_API_KEY" in readme


def test_readme_lists_what_v1_deliberately_does_not_do(readme: str):
    """把"有意不做"写进 README，避免后来的人当成遗漏而补上。"""
    assert "有意不做" in readme or "V1 有意不做" in readme


# ============================================================
# CI 只做跑测试（计划第 1008 行）
# ============================================================


def test_ci_only_runs_tests():
    ci = (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "pytest" in ci
    assert "alembic upgrade head" in ci
    # 不做发布/部署/多环境
    for forbidden in ("docker build", "docker push", "deploy", "release"):
        assert forbidden not in ci.lower(), f"CI 里出现了不属于 V1 的步骤：{forbidden}"


def test_ci_uses_real_postgres_and_redis():
    """集成测试依赖 PG 的 JSONB / TIMESTAMPTZ 行为，不能换 SQLite 顶替。"""
    ci = (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "postgres:15-alpine" in ci
    assert "redis:7-alpine" in ci


def test_ci_pins_python_313():
    """不用 3.14：Celery 官方支持到 3.13。"""
    ci = (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert '"3.13"' in ci
    assert '"3.14"' not in ci


def test_ci_has_no_real_secrets():
    ci = (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert not re.search(r"sk-[A-Za-z0-9]{16,}", ci), "CI 里不该有真实密钥"


# ============================================================
# 交付物齐全
# ============================================================


@pytest.mark.parametrize(
    "name",
    ["Dockerfile", ".dockerignore", "docker-compose.yml", "README.md",
     "requirements.txt", ".env.example", "alembic.ini", "pytest.ini"],
)
def test_delivery_files_exist(name: str):
    assert (PROJECT_ROOT / name).is_file(), f"缺少交付文件 {name}"


# ============================================================
# 凭据解析：README 让用户填 .env，那就必须真的能读到
# ============================================================


def test_api_key_is_readable_from_settings(monkeypatch):
    """`MODEL_PROVIDER_API_KEY` 写在 `.env` 里必须能被解析到。

    这里曾经只读 `os.environ`，而 **pydantic-settings 载入 `.env` 时不会把值
    写进 `os.environ`**。后果很具体：按 README 填好 `.env`、在宿主机直跑，
    会被判成"没配 Key"；只有在 Docker 里才碰巧能用，因为 compose 的
    `env_file:` 会把 `.env` 变成真正的环境变量。

    实测复现方式：把 `DSH_HOME` 指到一个空目录（切断 DSH 凭据文件那条路）。
    """
    from app.gateways import credentials

    class _FakeSettings:
        model_provider_api_key = "sk-from-dotenv-1234567890"

    monkeypatch.delenv("MODEL_PROVIDER_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr("app.config.get_settings", lambda: _FakeSettings())

    assert credentials.resolve_api_key() == "sk-from-dotenv-1234567890"


def test_api_key_falls_back_to_env_alias_then_dsh_file(monkeypatch, work_tmp):
    """Settings 没值时按「别名环境变量 → DSH 凭据文件」继续找。"""
    from app.gateways import credentials

    class _EmptySettings:
        model_provider_api_key = ""

    monkeypatch.setattr("app.config.get_settings", lambda: _EmptySettings())
    monkeypatch.delenv("MODEL_PROVIDER_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-alias-abcdefghijklmn")
    assert credentials.resolve_api_key() == "sk-alias-abcdefghijklmn"

    # 连别名也没有 → 落到 DSH 凭据文件
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    dsh_home = work_tmp / "dsh"
    dsh_home.mkdir(parents=True, exist_ok=True)
    (dsh_home / ".credentials.yaml").write_text(
        "refs:\n  MODEL_PROVIDER_API_KEY: sk-from-dsh-file-12345\n", encoding="utf-8"
    )
    monkeypatch.setenv("DSH_HOME", str(dsh_home))
    assert credentials.resolve_api_key() == "sk-from-dsh-file-12345"


def test_explicit_key_wins_over_everything(monkeypatch):
    from app.gateways import credentials

    class _FakeSettings:
        model_provider_api_key = "sk-from-dotenv-1234567890"

    monkeypatch.setattr("app.config.get_settings", lambda: _FakeSettings())
    assert credentials.resolve_api_key("sk-explicit") == "sk-explicit"
