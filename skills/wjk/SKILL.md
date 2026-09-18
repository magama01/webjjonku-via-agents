---
name: wjk
description: /wjk <요청> — 요청을 웹쫀쿠(ChatGPT 웹 Oracle) 미션으로 위임하고 결과를 회수함. 세션당 Project 1개 재사용.
---

# /wjk

`$ARGUMENTS`를 웹쫀쿠 미션으로 실행함.

`$ARGUMENTS`가 비어 있으면 무엇을 위임할지 사용자에게 묻고 멈춤.

## 전제

실행기 `${CODEX_HOME:-$HOME/.codex}/bin/chatgpt_oracle_run.py`가 있어야 함. 없으면
사용자에게 안내하고 멈춤:

```bash
git clone https://github.com/magama01/webjjonku-via-agents.git && cd webjjonku-via-agents && python3 install.py
```

## 절차

1. 미션 파일 작성: `<project-root>/.codex-tmp/web-mission.md`
   - 목표(`$ARGUMENTS`), 정확한 root, 읽기/쓰기 범위, 필요한 파일 경로, 산출물, 검증 조건만 씀.
   - 로컬 대화 전체나 파일 원문을 복사해 넣지 않음. 경로로 참조함.
2. 세션 ID는 `$WEBJJONKU_SESSION_ID`. 비어 있으면 `--session-id` 없이 단발 실행하고, 세션당 Project 재사용을 원하면 한 번 실행하라고 안내함 (새 세션부터 적용):
   ```bash
   sh "$(dirname "$(readlink -f ~/.claude/skills/wjk/SKILL.md)")/setup-claude-hook.sh"
   ```
   Codex/agy는 각자 `CODEX_SESSION_ID` / `AGY_CONVERSATION_ID`가 있으면 자동 감지됨.
3. dry-run 검증:
   ```bash
   python3 "${CODEX_HOME:-$HOME/.codex}"/bin/chatgpt_oracle_run.py \
     execute \
     --project-root "<project-root>" \
     --mission-path "<project-root>/.codex-tmp/web-mission.md" \
     --session-id "$WEBJJONKU_SESSION_ID" \
     --model gpt-5.6-sol \
     --effort extended \
     --dry-run
   ```
   통과하면 `--dry-run`만 빼고 같은 명령을 `run_in_background`로 실행함. 완료 알림을 기다림. 상태 폴링 반복·로그 전체 읽기·미션 재제출 금지.
4. 결과: run 디렉터리의 `output.md`를 읽고 짧은 결론 + 결과 경로를 보고함. `output.md`가 없거나 비어 있으면 완료로 취급하지 않음.
5. 대기 중 끊기면 새 실행 대신 `reconnect --run-dir "<run-dir>"`.

## 경계

- 사용자가 `/wjk`로 명시 요청한 작업만 웹에 넘김. 일반 대화를 자동 위임하지 않음.
- 인증, 승인 root, DevSpace, 브라우저 프로필은 건드리지 않음.
- 모델·effort는 사용자가 지정하면 그 값을 우선함.
