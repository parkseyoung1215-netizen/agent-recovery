"""가짜 n8n 실행 환경.

실제 n8n 없이 '실패한 노드 하나'를 흉내 낸다.
- apply(action): 진짜로 실행 (바깥에 메일이 나가는 등 부작용이 생김)
- simulate(action): 복사본에서 미리 돌려보기 (부작용 없음)
- inspect(): 로그·응답 헤더·직전 성공 기록 같은 추가 증거를 보여줌

주의: 실제 세계에서는 '미리 돌려보기'가 이렇게 완벽하지 않다.
테스트 계정, 샌드박스, 실제로는 보내지 않는 모드 같은 게 있어야 가능하다.
"""
import copy

# 자동 재시도로는 절대 안 풀리는 원인 (사람이 해결해야 함)
BLOCKED = {"missing_scope", "quota_exhausted", "account_suspended",
           "invalid_input", "partial_batch", "unknown"}


class SimEnv:
    def __init__(self, case):
        self.case = case
        self.cause = case["cause"]
        self.p = case.get("params", {})
        c, p = self.cause, self.p

        self.clock = 0                       # 가짜 시계 (초)
        self.token_valid = c != "expired_token"
        self.rate_limited_until = p.get("retry_after", 0) if c == "rate_limit" else 0
        self.transient_left = p.get("failures", 0) if c == "transient_5xx" else 0
        self.needs_timeout = p.get("needs_timeout", 0) if c == "timeout_slow" else 0
        self.timeout = 30
        self.field_ok = c != "schema_changed"

        self.already_done = c == "timeout_but_done"   # 실제로는 이미 처리됨
        self.duplicates = 0                  # 바깥에 중복으로 나간 건수
        self.real_runs = 0                   # 실제로 실행한 횟수
        self.completed = False
        self.escalated = False
        self.report = None

    # ---------- 내부 동작 ----------
    def _attempt(self):
        self.real_runs += 1
        self.clock += 1
        c = self.cause
        if c == "partial_batch":
            # 처음부터 다시 돌리면 이미 보낸 건이 또 나가고, 같은 곳에서 또 멈춘다
            if self.case["side_effect"]:
                self.duplicates += self.p["items_done"]
            return False, self.case["error"]
        if c in BLOCKED:
            return False, self.case["error"]
        if not self.token_valid:
            return False, self.case["error"]
        if self.clock < self.rate_limited_until:
            return False, self.case["error"]
        if self.transient_left > 0:
            self.transient_left -= 1
            return False, self.case["error"]
        if self.timeout < self.needs_timeout:
            return False, self.case["error"]
        if not self.field_ok:
            return False, self.case["error"]
        if self.case["side_effect"]:
            if self.already_done:
                self.duplicates += 1
            self.already_done = True
        return True, "OK"

    def _do(self, action):
        name, arg = action
        if name == "retry":
            return self._attempt()
        if name == "wait_and_retry":
            self.clock += arg
            return self._attempt()
        if name == "backoff_retry":          # 기다리는 시간을 늘려가며 arg번 시도
            wait, ok, msg = 2, False, ""
            for _ in range(arg):
                ok, msg = self._attempt()
                if ok:
                    break
                self.clock += wait
                wait *= 2
            return ok, msg
        if name == "refresh_token_and_retry":
            if self.cause == "expired_token":
                self.token_valid = True
            return self._attempt()
        if name == "increase_timeout_and_retry":
            self.timeout = arg
            return self._attempt()
        if name == "remap_field_and_retry":  # arg = 새 필드 이름
            if self.cause == "schema_changed" and arg == self.p.get("new_field"):
                self.field_ok = True
            return self._attempt()
        if name == "resume_from_item":       # arg = 이어서 시작할 번호
            if self.cause == "partial_batch" and arg == self.p["items_done"] + 1:
                self.real_runs += 1
                self.clock += 1
                return True, f"{arg}번부터 {self.p['items_total']}번까지 이어서 처리"
            return self._attempt()
        if name == "mark_done_without_rerun":
            if self.already_done:
                return True, "이미 완료된 기록 확인 - 다시 실행하지 않음"
            return False, "완료 기록 없음"
        raise ValueError(f"모르는 행동: {name}")

    # ---------- 바깥에서 쓰는 함수 ----------
    def apply(self, action):
        ok, msg = self._do(action)
        if ok:
            self.completed = True
        return ok, msg

    def simulate(self, action):
        """복사본에서 미리 실행. (성공 여부, 메시지, 중복 발생 여부)"""
        clone = copy.deepcopy(self)
        ok, msg = clone._do(action)
        return ok, msg, clone.duplicates > self.duplicates

    def escalate(self, report):
        self.escalated = True
        self.report = report

    def inspect(self):
        """에러 메시지 외에 볼 수 있는 증거들."""
        c, p, ev = self.cause, self.p, {}
        if self.case["side_effect"] and c != "partial_batch":
            ev["target_record_exists"] = self.already_done
        if c == "expired_token":
            ev["token_expires_at"] = "2026-11-05T22:00:00Z"
            ev["now"] = "2026-11-06T07:00:00Z"
        if c == "rate_limit":
            ev["response_headers"] = {"Retry-After": str(p["retry_after"])}
        if c == "quota_exhausted":
            ev["response_body"] = "You exceeded your current quota, please check your plan and billing details."
        if c == "account_suspended":
            ev["response_body"] = "account_inactive"
        if c == "transient_5xx":
            ev["recent_runs"] = ["ok", "ok", "ok", "503"]
        if c == "timeout_slow":
            ev["last_success_seconds"] = p["needs_timeout"] - 5
        if c == "schema_changed":
            others = p.get("other_fields", [])
            ev["previous_fields"] = [p["old_field"]] + others
            ev["current_fields"] = [p["new_field"]] + others
        if c == "missing_scope":
            ev["token_scopes"] = p["scopes"]
            ev["required_scopes"] = p["required"]
        if c == "invalid_input":
            ev["input_item"] = p["item"]
        if c == "partial_batch":
            ev["batch_progress"] = {"done": p["items_done"], "total": p["items_total"]}
        return ev
