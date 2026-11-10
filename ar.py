"""Agent Recovery - 명령어 하나로 쓰기

  python3 ar.py check [워크플로ID]   실패하기 전에 중복 위험 찾기
  python3 ar.py watch                실패를 지켜보다가 복구 계획 만들기 (승인 페이지: 127.0.0.1:8100)
  python3 ar.py recover <실행ID>      실패한 실행 하나를 직접 복구 (--yes 면 확인 없이)
  python3 ar.py list                 최근 실패한 실행 목록
  python3 ar.py services             services.json에 등록된 서비스 보기
  python3 ar.py chaos ...            배포 전 실패 주입 테스트 (prepare / serve / report / reset)

필요한 것: 같은 폴더의 .env 파일에 N8N_API_KEY=... (n8n 읽기 전용 키면 충분)
"""
import sys

USAGE = __doc__


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(USAGE)
        return
    cmd, rest = sys.argv[1], sys.argv[2:]
    if cmd == "check":
        import check
        sys.argv = ["check.py", *rest]
        check.main()
    elif cmd == "watch":
        import watch
        watch.main()
    elif cmd == "recover":
        if not rest:
            sys.exit("실행 ID가 필요해요. 예: python3 ar.py recover 12   (목록은 python3 ar.py list)")
        import recover
        sys.argv = ["recover.py", *rest]
        recover.main()
    elif cmd == "list":
        import recover
        sys.argv = ["recover.py", "--list"]
        recover.main()
    elif cmd == "chaos":
        import chaos
        chaos.main(rest)
    elif cmd == "services":
        import recover
        svcs = recover.load_services()
        if not svcs:
            print("services.json에 등록된 서비스가 없어요.")
        for s in svcs:
            print(f"- {s['name']}\n    요청 주소: {s['match']}\n    확인 방법: {s.get('lookup') or '없음 (확인 불가 → 사람에게 넘김)'}"
                  f"\n    반영: {s.get('lookup_mode', 'eventual')}, 대기 {s.get('settle_seconds', 30)}초")
    else:
        print(f"모르는 명령어: {cmd}\n")
        print(USAGE)


if __name__ == "__main__":
    main()
