"""一把跑完仓库里所有自测。

为什么要有这个：自测文件已经十几份（hook 层、执行链、数据层、策略层、IO/LLM、
新功能各一份），散着跑很容易「改完只跑了自己那份」，漏掉回归。

用法：
    .venv\\Scripts\\python.exe selftest_all.py          # 跑全部
    .venv\\Scripts\\python.exe selftest_all.py -v       # 同时打印每个子测试的尾部输出

规矩（和项目其它自测一致）：
  * **不需要微信、不碰 hook（127.0.0.1:30001）、不联网**；
  * 全部通过 exit 0，任一失败 exit 1，并把失败名单印在最后。
"""
import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# 要跑的自测文件：selftest_*.py + executor 的两份 + executor.py 自带自测
EXTRA = ["executor_selftest.py", "selftest_executor_chain.py"]
SELF = os.path.basename(os.path.abspath(__file__))
TIMEOUT = 600


def targets():
    out = []
    for p in sorted(glob.glob(os.path.join(HERE, "selftest_*.py"))):
        name = os.path.basename(p)
        if name == SELF:
            continue                      # 别把自己递归跑起来
        out.append(name)
    for name in EXTRA:
        if os.path.isfile(os.path.join(HERE, name)) and name not in out:
            out.append(name)
    return out


def main(argv):
    verbose = "-v" in argv
    files = targets()
    print("=" * 66)
    print(f"跑全部自测，共 {len(files)} 份（无微信 / 不碰 hook / 不联网）")
    print("=" * 66)
    failed = []
    for name in files:
        try:
            r = subprocess.run([sys.executable, name], cwd=HERE,
                               capture_output=True, timeout=TIMEOUT)
            code = r.returncode
        except subprocess.TimeoutExpired:
            code = -1
            r = None
        tail = ""
        if r is not None:
            text = (r.stdout or b"").decode("utf-8", "ignore").strip().splitlines()
            tail = text[-1].strip() if text else ""
        flag = "✅" if code == 0 else "❌"
        print(f"  {flag} {name:<32} exit={code}  {tail[:48]}")
        if code != 0:
            failed.append(name)
            if verbose and r is not None:
                for line in (r.stdout or b"").decode("utf-8", "ignore").splitlines()[-15:]:
                    print(f"       | {line}")
                for line in (r.stderr or b"").decode("utf-8", "ignore").splitlines()[-10:]:
                    print(f"       ! {line}")
    print("=" * 66)
    if failed:
        print(f"有 {len(failed)} 份失败 ❌：{'、'.join(failed)}")
        if not verbose:
            print("加 -v 看失败明细；或单独跑其中一份看完整输出。")
        print("=" * 66)
        return 1
    print(f"全部 {len(files)} 份通过 ✅")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
