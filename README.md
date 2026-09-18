<p align="center">
  <img src="docs/assets/brand/banner.svg" alt="Codex Web GPT Automation" width="100%">
</p>

# 웹쫀쿠 via Agents

[Codex Web GPT Automation](https://github.com/ventianima-lab/codex-web-gpt-automation)
([최신 릴리스](https://github.com/ventianima-lab/codex-web-gpt-automation/releases/latest) ·
<img alt="Release" src="https://img.shields.io/github/v/tag/ventianima-lab/codex-web-gpt-automation?sort=semver&label=release">)의 포크입니다.
원본은 로컬 프로젝트를 **웹 ChatGPT**(브라우저 로그인 세션)에 안전하게 연결해서, ChatGPT가 내 프로젝트
파일을 읽고 분석·작업하게 만드는 도구입니다. 이 포크는 그걸 **Claude Code · Codex · agy(Antigravity)에서
`/wjk <요청>` 한 줄로** 쓸 수 있게 다듬은 버전입니다.

> 커뮤니티 프로젝트이며 OpenAI 공식 제품이 아닙니다. ChatGPT 로그인·앱 등록은 본인이 직접 합니다.

## 되는 것

- 에이전트(Claude/Codex/agy)에게 `/wjk 이 프로젝트 구조 분석해줘` 라고 하면
- 웹 ChatGPT가 내 프로젝트를 읽고 답을 만들고
- 결과가 로컬 파일로 돌아옵니다.
- 한 에이전트 세션 안에서는 ChatGPT Project 하나를 만들어 계속 재사용합니다.

## 준비물

| 항목 | 확인 방법 |
|---|---|
| ChatGPT 계정 (브라우저 로그인 가능) | chatgpt.com 로그인 |
| Node.js 24~26 | `node -v` |
| Python 3 | `python3 --version` (Windows는 `python --version`) |
| Git | `git --version` |
| 공개 HTTPS 주소 하나 | 아래 3단계에서 Tailscale / Cloudflare / ngrok 중 하나 |

Windows는 아래 명령에서 `python3` 대신 `python`을 쓰세요.

## 설치

### 1. 받기

```bash
git clone https://github.com/magama01/webjjonku-via-agents.git
cd webjjonku-via-agents
```

### 2. 실행기 설치

```bash
python3 install.py
python3 doctor.py
```

`doctor.py`가 오류 없이 끝나면 통과입니다. `~/.codex/bin`에 실행기가 들어갑니다.

### 3. 공개 주소 만들기

ChatGPT가 내 컴퓨터의 `http://127.0.0.1:7676`에 접속할 수 있게 고정 HTTPS 주소가 필요합니다.
셋 중 **하나만** 고르세요. 처음이면 3-1 Tailscale이 가장 쉽습니다 (설치 스크립트가 다 해줌).

#### 3-1. Tailscale (추천)

1. [tailscale.com](https://tailscale.com) 가입 후 앱 설치·로그인
2. 관리 콘솔에서 **MagicDNS**, **HTTPS**, **Funnel** 켜기 (DNS → Enable MagicDNS / Enable HTTPS, Access controls → Funnel)
3. 내 기기 이름 확인: `tailscale status` 에 나오는 `내기기.내테일넷.ts.net`
4. 4단계로 진행. 주소는 자동으로 잡힙니다.

#### 3-2. Cloudflare Tunnel

내 도메인이 Cloudflare에 있어야 합니다.

```bash
cloudflared tunnel login
cloudflared tunnel create webjjonku
cloudflared tunnel route dns webjjonku mcp.내도메인.com
cloudflared tunnel run --url http://127.0.0.1:7676 webjjonku
```

동작 확인 후 부팅 시 자동 실행으로 등록:

```bash
sudo cloudflared service install
```

주소는 `https://mcp.내도메인.com/mcp` 입니다.

#### 3-3. ngrok

무료 계정도 고정 도메인 1개를 줍니다 (대시보드 → Domains).

```bash
ngrok config add-authtoken <내 토큰>
ngrok http --url=https://내이름.ngrok.app 7676
```

동작 확인 후 부팅 시 자동 실행으로 등록:

```bash
ngrok service install --config ~/.config/ngrok/ngrok.yml
```

주소는 `https://내이름.ngrok.app/mcp` 입니다.

### 4. 연결 마법사

ChatGPT에 읽히고 싶은 프로젝트 폴더를 `--root`로 줍니다. 여러 개면 `--root`를 반복하세요.

```bash
# Tailscale
python3 onboard.py start --provider tailscale --root /내/프로젝트

# Cloudflare
python3 onboard.py start --provider cloudflare --public-url https://mcp.내도메인.com/mcp --root /내/프로젝트

# ngrok
python3 onboard.py start --provider ngrok --public-url https://내이름.ngrok.app/mcp --root /내/프로젝트
```

이후는 마법사가 한 단계씩 안내합니다. 화면 지시대로 하고, 끝날 때마다:

```bash
python3 onboard.py next
```

마법사가 시키는 것 요약:

| 단계 | 내가 할 일 |
|---|---|
| DevSpace 초기화 | 터미널에 뜨는 질문에 프로젝트 폴더·공개 주소 입력. **Owner 비밀번호가 출력되면 따로 적어두기** (다시 안 보여줌, 어디에도 붙여넣지 않기) |
| Oracle 브라우저 로그인 | 새로 뜨는 크롬 창에서 ChatGPT 로그인 한 번 |
| ChatGPT 앱 등록 | chatgpt.com → 설정 → 앱/커넥터 → Developer Mode 켜기 → 새 앱: 이름 `codex`, URL `https://내주소/mcp` → Owner 비밀번호 입력해서 승인 |
| 확인 | ChatGPT에서 `@codex`로 파일 하나 읽어보기 |

중간에 끊겼으면 `python3 onboard.py resume` 으로 이어갑니다.

### 5. 에이전트에 스킬 추가

```bash
npx skills add magama01/webjjonku-via-agents -s wjk -g -a claude-code -a codex -a antigravity
```

**Claude Code만 추가로** 한 번 (세션당 ChatGPT Project 1개 재사용하려면):

```bash
sh ~/.agents/skills/wjk/setup-claude-hook.sh
```

새 세션부터 적용됩니다.

## 사용

에이전트 대화창에서:

```
/wjk 이 프로젝트의 인증 흐름을 분석하고 문제점 정리해줘
```

에이전트가 미션 파일을 만들고 → 웹 ChatGPT에 보내고 → 결과 `output.md` 경로와 요약을 돌려줍니다.
같은 세션에서 다시 `/wjk` 하면 같은 ChatGPT Project를 이어서 씁니다.

## 문제가 생기면

```bash
python3 doctor.py           # 현재 상태 진단
python3 onboard.py status   # 연결 단계 어디까지 됐는지
python3 onboard.py resume   # 마법사 이어하기
```

- 브라우저 로그인 풀림 → 마법사의 Oracle 로그인 단계 다시
- ChatGPT 앱이 도구를 못 찾음 → 앱 상세에서 `Refresh` / 안 되면 `Reconnect`. 앱을 새로 만들지 마세요.
- 재부팅 후 안 됨 → 3단계 터널이 자동 실행으로 등록됐는지 확인

## 안전 규칙

ChatGPT 앱 `codex` 등록은 준비가 끝난 뒤 **최초 한 번 수동 등록**하는 절차입니다.
ChatGPT 설정·앱 목록·권한·삭제·선택 UI를 자동화하지 않습니다. 승인한 프로젝트 폴더 밖은 읽지 않고,
결과를 저장하기 전에는 브라우저 탭을 닫지 않으며, 오류가 나도 같은 미션을 자동으로 다시 보내지 않습니다.
비밀번호·토큰·브라우저 프로필은 저장소에 넣지 않습니다. 전체 규칙은 [공통 자동화 규칙](docs/AUTOMATION_POLICY.md).

## 더 알아보기

- [외부 에이전트 연동 상세](docs/AGENT_INTEGRATION.md)
- [최초 설치 상세 (원본)](docs/FIRST_INSTALL.md) · [DevSpace + Tailscale](docs/DEVSPACE_TAILSCALE_SETUP.md)
- [공통 자동화 규칙](docs/AUTOMATION_POLICY.md) · [문서 전체](docs/README.md)
- [English](README.en.md)

## 라이선스

[MIT](LICENSE). Oracle·DevSpace 등 제3자 구성요소는 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
