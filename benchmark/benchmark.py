"""세 가지 복구 방식을 같은 실패 문제집으로 비교한다.

실행: python3 benchmark.py
결과: 화면에 요약표, results.md에 케이스별 상세
"""
import json
from pathlib import Path

from sim_env import SimEnv
from strategies import STRATEGIES

HERE = Path(__file__).parent
cases = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))


def judge(case, env):
    """올바르게 처리했는가? 중복이 하나라도 생기면 실패."""
    if env.duplicates > 0:
        return False
    if case["expected"] == "recover":
        return env.completed
    return env.escalated and not env.completed


summary, details = {}, []
for name, strategy in STRATEGIES.items():
    s = {"correct": 0, "recovered": 0, "escalated_ok": 0, "dups": 0,
         "runs": 0, "diag_ok": 0}
    for case in cases:
        env = SimEnv(case)
        out = strategy(env)
        ok = judge(case, env)
        s["correct"] += ok
        s["recovered"] += ok and case["expected"] == "recover"
        s["escalated_ok"] += ok and case["expected"] == "escalate"
        s["dups"] += env.duplicates
        s["runs"] += env.real_runs
        s["diag_ok"] += out["diagnosis"] == case["cause"]
        details.append((name, case, ok, env, out))
    summary[name] = s

n = len(cases)
n_rec = sum(c["expected"] == "recover" for c in cases)
n_esc = n - n_rec

lines = [
    "| 방식 | 올바른 처리 | 복구 성공 | 올바른 보고 | 중복 발생(건) | 실제 실행 횟수 | 원인 진단 정확 |",
    "|---|---|---|---|---|---|---|",
]
for name, s in summary.items():
    lines.append(
        f"| {name} | {s['correct']}/{n} | {s['recovered']}/{n_rec} | {s['escalated_ok']}/{n_esc} "
        f"| {s['dups']} | {s['runs']} | {s['diag_ok']}/{n} |"
    )
table = "\n".join(lines)
print(table)

md = ["# 벤치마크 결과", "", table, "", "## 케이스별 상세", ""]
for name, case, ok, env, out in details:
    mark = "O" if ok else "X"
    md.append(f"- **{mark}** [{name}] #{case['id']} {case['title']} "
              f"(정답: {case['expected']}, 진단: {out['diagnosis']}, 중복: {env.duplicates})")
    for t in out["trace"]:
        md.append(f"    - {t}")
(HERE / "results.md").write_text("\n".join(md), encoding="utf-8")
print("\n상세 결과 → results.md")
