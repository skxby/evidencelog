"""14-1 的真机验证：**全新 clone** 一份仓库，按 README 一键起全栈并完成一次真实分析。

验收原文是「全新环境 clone 后，按 README 能在本机一键起全栈并完成一次真实分析」。
不真 clone 的话，这条永远只能算"本机复现" —— 所以这里老老实实走一遍：

  ① `git clone` 到工作区内的临时目录（只带版本库里的文件，不带 .env / 数据卷）
  ② 按 README「准备配置」把 .env 放进去（.env 本来就不进版本库，这一步是用户的动作）
  ③ 把**原栈** `docker compose down`（保留数据卷），腾出端口与容器名
  ④ 在 clone 目录里 `docker compose up -d --build`（README 的一键起全栈）
  ⑤ 在 clone 目录里跑 `node .dsh/final_verify.cjs` —— 16 项端到端，含一次真实分析
  ⑥ clone 目录 `docker compose down`，再把原栈起回来，确认数据与健康都还在

用法：python .dsh/fresh_clone_check.py [--keep] [--remote <url>]
      # --keep：跑完不删 clone 目录
      # --remote：改成从远端 clone（默认从本地工作区 clone）。仓库公开后，
      #           `--remote https://github.com/skxby/evidencelog` 验的才是
      #           "别人 clone 到的东西能不能跑"，而不是"我本机这份能不能跑"。
      #           网络受限时可用 GIT_CONFIG_COUNT/GIT_CONFIG_KEY_n/GIT_CONFIG_VALUE_n
      #           给 git 传代理与 TLS 后端配置（脚本原样继承环境变量）。
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
CLONE = ROOT / ".fresh-clone"
EVIDENCE = ROOT / ".dsh" / "runtime_evidence.json"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def run(cmd: list[str], cwd: pathlib.Path, *, timeout: int = 900) -> tuple[int, str]:
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def healthy(url: str = "http://127.0.0.1:8000/healthz", *, tries: int = 40) -> bool:
    for _ in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - 还没起来就继续等
            time.sleep(3)
    return False


def merge(patch: dict) -> None:
    payload = json.loads(EVIDENCE.read_text(encoding="utf-8")) if EVIDENCE.exists() else {}
    payload.update(patch)
    payload["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    EVIDENCE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )


def main(argv: list[str]) -> int:
    facts: dict = {}
    if CLONE.exists():
        shutil.rmtree(CLONE, ignore_errors=True)

    # 从哪 clone：默认本地工作区；给了 --remote 就从远端（公开仓库）拉
    remote = str(ROOT)
    if "--remote" in argv:
        remote = argv[argv.index("--remote") + 1]

    # ① clone
    code, log = run(["git", "clone", "--quiet", remote, str(CLONE)], cwd=ROOT)
    facts["clone_remote"] = remote
    facts["clone_exit"] = code
    facts["clone_files"] = len(list(CLONE.rglob("*"))) if CLONE.exists() else 0
    facts["clone_has_env"] = (CLONE / ".env").exists()  # 应当为 False（密钥不进版本库）
    facts["clone_head"] = run(["git", "rev-parse", "HEAD"], cwd=CLONE)[1].strip()[:12] \
        if CLONE.exists() else ""
    print(f"① clone（{remote}）退出码 {code}，HEAD {facts['clone_head']}，"
          f"文件 {facts['clone_files']} 个，clone 里有 .env 吗：{facts['clone_has_env']}")
    if code != 0:
        facts["note"] = f"clone 失败：{log.strip().splitlines()[-3:]}"
        print(json.dumps(facts, ensure_ascii=False, indent=1))
        merge({"fresh_clone_check": facts})
        return 1

    # ② 准备配置（README 第 ① 步）
    shutil.copy2(ROOT / ".env", CLONE / ".env")
    facts["env_copied"] = True
    print("② 已按 README 放入 .env")

    # ③ 原栈 down（保留数据卷）
    code, log = run(["docker", "compose", "down"], cwd=ROOT)
    facts["original_down_exit"] = code
    print(f"③ 原栈 down 退出码 {code}")

    try:
        # ④ 一键起全栈（README 第 ② 步）
        code, log = run(["docker", "compose", "up", "-d", "--build"], cwd=CLONE, timeout=1800)
        facts["up_exit"] = code
        facts["up_tail"] = log.strip().splitlines()[-3:]
        ok = healthy()
        facts["healthy_after_up"] = ok
        print(f"④ up 退出码 {code}，健康检查：{ok}")
        if not ok:
            facts["note"] = "一键起全栈没起来，后续步骤跳过"
            return 1

        # ⑤ 真实分析（16 项端到端）
        code, log = run(["node", ".dsh/final_verify.cjs"], cwd=CLONE, timeout=900)
        match = re.search(r"(\d+)/(\d+) 项通过", log)
        facts["e2e_exit"] = code
        facts["e2e_passed"] = int(match.group(1)) if match else 0
        facts["e2e_total"] = int(match.group(2)) if match else 0
        cost = re.search(r"cost=¥([\d.]+)", log)
        facts["e2e_cost"] = float(cost.group(1)) if cost else None
        facts["e2e_tail"] = log.strip().splitlines()[-6:]
        print(f"⑤ E2E：{facts['e2e_passed']}/{facts['e2e_total']}，"
              f"真实分析花费 ¥{facts['e2e_cost']}")
    finally:
        # ⑥ 收拾：clone 栈 down，原栈起回来
        run(["docker", "compose", "down"], cwd=CLONE)
        code, log = run(["docker", "compose", "up", "-d"], cwd=ROOT)
        restored = healthy()
        facts["restored_exit"] = code
        facts["original_restored"] = restored
        print(f"⑥ 原栈已恢复：{restored}")
        if "--keep" not in argv:
            shutil.rmtree(CLONE, ignore_errors=True)

    facts["fresh_clone_ok"] = (
        facts.get("clone_exit") == 0
        and facts.get("clone_has_env") is False
        and facts.get("healthy_after_up") is True
        and facts.get("e2e_passed") == facts.get("e2e_total")
        and facts.get("e2e_total")
        and facts.get("original_restored") is True
    )
    merge({"fresh_clone_check": facts})
    print(json.dumps(facts, ensure_ascii=False, indent=1))
    return 0 if facts["fresh_clone_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
