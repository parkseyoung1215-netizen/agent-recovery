"""복구 방식 세 가지.

1. naive_retry   : n8n 기본 '실패 시 재시도'처럼 그냥 3번 다시 돌린다.
2. one_shot      : 에러 메시지만 보고 한 번 판단해서 한 번 고친다.
                   ("에러 로그를 AI한테 한 번 물어보기"에 해당)
3. recovery_loop : Reasoning Lab 방식.
                   증거 수집 → 원인 가설 여러 개 → 미리 돌려서 검증
                   → 안전한 것만 실제 실행 → 안 되면 다음 가설 → 그래도 안 되면 사람에게 보고
"""


def naive_retry(env, tries=3):
    trace = []
    for i in range(tries):
        if i > 0:
            env.clock += 1                   # n8n 기본 대기 1초
        ok, msg = env.apply(("retry", None))
        trace.append(f"retry → {msg}")
        if ok:
            return {"diagnosis": None, "trace": trace}
    env.escalate("재시도 3번 실패")
    return {"diagnosis": None, "trace": trace}


# 에러 메시지 키워드 → (추정 원인, 고치는 방법)
ONE_SHOT_TABLE = [
    ("401", "expired_token", ("refresh_token_and_retry", None)),
    ("403", "expired_token", ("refresh_token_and_retry", None)),
    ("429", "rate_limit", ("wait_and_retry", 60)),
    ("503", "transient_5xx", ("retry", None)),
    ("timed out", "timeout_slow", ("increase_timeout_and_retry", 120)),
    ("Cannot read properties", "schema_changed", ("remap_field_and_retry", None)),  # 새 필드 이름은 모름
    ("400", "invalid_input", None),
]


def one_shot(env):
    err = env.case["error"]
    for key, diag, action in ONE_SHOT_TABLE:
        if key in err:
            if action is None:
                env.escalate(f"추정 원인: {diag}")
                return {"diagnosis": diag, "trace": ["사람에게 보고"]}
            ok, msg = env.apply(action)
            if not ok:
                env.escalate(f"추정 원인 {diag}, 수정 실패")
            return {"diagnosis": diag, "trace": [f"{action[0]} → {msg}"]}
    ok, msg = env.apply(("retry", None))
    if not ok:
        env.escalate("원인 모름")
    return {"diagnosis": "unknown", "trace": [f"retry → {msg}"]}


def hypotheses(err, ev):
    """(원인, 고치는 방법 또는 None=사람 필요, 근거) 목록. 앞에 있을수록 우선."""
    H = []
    # --- 증거가 직접 가리키는 것 먼저 ---
    if ev.get("target_record_exists"):
        H.append(("timeout_but_done", ("mark_done_without_rerun", None),
                  "실패로 보이지만 대상 기록이 이미 존재함"))
    if "batch_progress" in ev:
        b = ev["batch_progress"]
        H.append(("partial_batch", ("resume_from_item", b["done"] + 1),
                  f"{b['total']}건 중 {b['done']}건 처리 후 중단. 처음부터 다시 돌리면 {b['done']}건 중복"))
    if "required_scopes" in ev:
        missing = set(ev["required_scopes"]) - set(ev["token_scopes"])
        if missing:
            H.append(("missing_scope", None, f"토큰에 없는 권한: {sorted(missing)}"))
    body = ev.get("response_body", "")
    if "quota" in body:
        H.append(("quota_exhausted", None, "결제·사용 한도 소진 - 기다려도 안 풀림"))
    if "inactive" in body or "disabled" in body:
        H.append(("account_suspended", None, "계정 비활성화"))
    if "input_item" in ev:
        empty = [k for k, v in ev["input_item"].items() if v in ("", None)]
        if empty:
            H.append(("invalid_input", None, f"입력 데이터 빈 칸: {empty}"))
    if ev.get("token_expires_at", "9") < ev.get("now", "0"):
        H.append(("expired_token", ("refresh_token_and_retry", None), "토큰 만료 시각이 지남"))
    ra = ev.get("response_headers", {}).get("Retry-After")
    if ra:
        H.append(("rate_limit", ("wait_and_retry", int(ra) + 1), f"서버가 {ra}초 뒤에 다시 하라고 함"))
    if "previous_fields" in ev:
        gone = set(ev["previous_fields"]) - set(ev["current_fields"])
        new = set(ev["current_fields"]) - set(ev["previous_fields"])
        if len(gone) == 1 and len(new) == 1:
            g, n = gone.pop(), new.pop()
            H.append(("schema_changed", ("remap_field_and_retry", n), f"필드 이름이 {g} → {n}로 바뀜"))
    if ev.get("last_success_seconds", 0) > 30:
        s = ev["last_success_seconds"]
        H.append(("timeout_slow", ("increase_timeout_and_retry", s * 2), f"최근 성공 때 {s}초 걸림"))
    if "recent_runs" in ev:
        H.append(("transient_5xx", ("backoff_retry", 5), "평소엔 잘 되다가 갑자기 실패"))
    # --- 증거가 없을 때의 일반 후보 ---
    if "503" in err or "502" in err:
        H.append(("transient_5xx", ("backoff_retry", 5), "서버 일시 장애 코드"))
    if "429" in err:
        H.append(("rate_limit", ("wait_and_retry", 60), "요청 한도 코드"))
    if "401" in err:
        H.append(("expired_token", ("refresh_token_and_retry", None), "인증 실패 코드"))
    if "timed out" in err:
        H.append(("timeout_slow", ("increase_timeout_and_retry", 120), "타임아웃"))
    H.append(("unknown", None, "알려진 패턴과 맞지 않음"))

    seen, out = set(), []
    for h in H:                              # 같은 행동 중복 제거
        key = h[1] if h[1] else h[0]
        if key not in seen:
            seen.add(key)
            out.append(h)
    return out


def recovery_loop(env, max_hypotheses=4):
    ev = env.inspect()
    trace, approvals = [], 0
    for diag, action, why in hypotheses(env.case["error"], ev)[:max_hypotheses]:
        if action is None:
            env.escalate(f"원인: {diag} / 근거: {why} / 시도: {trace}")
            trace.append(f"[{diag}] 사람 필요 - {why}")
            return {"diagnosis": diag, "trace": trace, "approvals": approvals}
        ok, msg, dup = env.simulate(action)
        if not ok or dup:
            trace.append(f"[{diag}] 미리 실행 실패{' (중복 위험)' if dup else ''} → 다음 가설")
            continue
        if env.case["side_effect"] and action[0] != "mark_done_without_rerun":
            approvals += 1                   # 실제 서비스에선 여기서 사람 승인을 받음
        ok, msg = env.apply(action)
        trace.append(f"[{diag}] {why} → {action[0]} → {msg}")
        if ok:
            return {"diagnosis": diag, "trace": trace, "approvals": approvals}
    env.escalate(f"모든 가설 실패 / 시도: {trace}")
    return {"diagnosis": "unknown", "trace": trace, "approvals": approvals}


STRATEGIES = {
    "단순 재시도": naive_retry,
    "한 번 묻고 고치기": one_shot,
    "가설-검증 루프": recovery_loop,
}
