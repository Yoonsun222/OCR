# 회사 PC에서 개발 이어받기

현재 통합 브랜치는 `feature/track-c-integration`이다. Windows 오프라인 도구, Excel 표·차트 실험(Track B), 도면 라벨 위치 실험(Track A), 혼합 검색(Track C) 작업이 모두 이 브랜치에 포함돼 있다.

## 1. 저장소 받기

회사에서 허용된 Git 접근 경로를 사용한다. 새로 받는 경우 Windows PowerShell에서 실행한다.

```powershell
git clone --branch feature/track-c-integration https://github.com/Yoonsun222/OCR.git RagLab
Set-Location RagLab
git status --short --branch
py -3.11 --version
```

이미 복제했다면 작업 트리가 깨끗한 것을 확인한 뒤 `git fetch origin`, `git switch feature/track-c-integration`, `git pull --ff-only` 순서로 갱신한다. 처음 전환할 때 브랜치가 없으면 `git switch -c feature/track-c-integration --track origin/feature/track-c-integration`을 사용한다. 회사 PC에서 GitHub 접근이 제한되면 승인된 반입 절차로 같은 브랜치를 받는다.

개발 테스트는 회사가 승인한 Python 3.11과 pytest 8.3.5 환경에서 실행한다. pytest의 설치와 반입은 회사에서 승인한 방법을 따른다.

```powershell
py -3.11 -m pytest tests/track_c/test_retrieval.py tests/security/test_authorization.py -q
py -3.11 -m pytest -q
```

운영 설치에는 별도 절차가 있다. [시작 안내](START-HERE.md), [오프라인 설치](OFFLINE-INSTALL.md), [번들 신뢰 설정](WINDOWS-BUNDLE-TRUST.md)을 사용한다.

## 2. 현재 구현 상태

| 영역 | 현재 상태 |
| --- | --- |
| 공통 계약 및 Windows 툴킷 | 근거·권한·세대·삭제 계약, 번들 검증, `doctor`, 합성 데모, 진단, 재현 번들, 설치·복구·제거를 구현했다. 회사 서명 번들과 회사 환경의 검증은 남아 있다. |
| Track B | CSV/XLSX의 여러 시트와 표, 네이티브 차트의 계열·범례·원본 셀 범위를 읽는다. 저장된 수식 결과만 사용하며, 명시 셀 조회와 제한된 숫자 집계를 합성 자료로 검증했다. [지원 범위](../track-b-native-contract.md) |
| Track A | 합성 SVG와 기록된 OCR 토큰에서 한·영·일 명시 라벨을 찾고, 승인된 파일의 라벨·부품 영역을 연결한다. 학습된 실제 OCR 엔진과 사내 문서 저장소는 연결 전이다. [평가 범위](../track-a-evaluation.md) |
| Track C | 작업 1~4는 독립 리뷰를 통과했다. 작업 5는 채널 검색 및 제한 시간·오류 경계 보완까지 체크포인트로 보존했다. 보완 변경의 독립 재리뷰와 작업 6~9가 남아 있다. |

인수인계 시점의 Windows Python 3.11 전체 테스트는 885개 통과, 7개 건너뜀이었다. 건너뛴 항목은 로컬 Windows의 심볼릭 링크 생성 권한에 의존하는 테스트다.

## 3. 회사 PC에서 다음에 구현할 순서

세부 요구사항과 검증 목록은 [Track C 구현 계획](../superpowers/plans/2026-11-24-track-c-mixed-document-retrieval.md)에 있다.

1. **작업 5 재리뷰.** `src/raglab/track_c/retrieval.py`와 `tests/track_c/test_retrieval.py`의 마지막 변경을 검토한다. 채널 확인과 후보 변환이 작업 스레드 안에서만 실행되고, 제한 시간이 잘못되면 검색을 시작하지 않는지 확인한다. 모든 어댑터가 허용된 문서 ID와 정확한 버전으로 먼저 범위를 좁히고, 반환 후보도 출처가 맞는 경우만 통과해야 한다. 지연된 채널이 반복 호출로 작업을 무한히 쌓거나 오류 원문을 드러내지 않아야 한다.
2. **작업 6 검색 결과 통합.** 순위 기반으로 채널 결과를 합치고 같은 근거를 중복 집계하지 않는다. 문서가 다른데 셀 주소만 같은 결과를 하나로 합치지 않는다. 같은 문서의 버전 간 명시 값 충돌은 상위 3건 제한 전에 판정한다. 상대 순위 점수와 답변 가능성을 보여 주는 절대 근거 점수를 구분한다. 매번 1.0으로 정규화된 최고 순위 점수를 작업 7의 신뢰도 기준으로 사용하면 약한 근거에도 답하게 된다.
3. **작업 7 근거 기반 답변.** 인용 가능한 원본을 연결하고, 근거 없음·낮은 신뢰도·값 충돌 상태를 분리한다. 차트의 범례는 검색 텍스트이며 수치가 아니다. 문서 안에 적힌 명령문은 데이터로 다룬다. 검증되지 않은 도면의 위치나 숫자는 확정하지 않는다.
4. **작업 8 조회 서비스 연결.** 조회 시작 시 인덱스 세대와 문서별 권한을 고정하고, 자료를 읽을 때와 답변·하이라이트를 내보낼 때 다시 확인한다. 권한이 바뀌면 근거와 진단을 비우고, 재시도는 계획이 정한 조건에서 한 번만 수행한다. Track A의 신뢰된 데이터 제공자를 회사의 승인된 원본 저장소에 연결할 지점을 확인한다.
5. **작업 9 통합 평가.** 합성 자료로 검색·근거 일치·답변 보류·접근 철회·지연 테스트를 먼저 통과시킨다. 다음으로 회사의 승인된 Windows CPU 환경에서 별도 평가 세트를 사용해 정확도, 오답·누락, P95, 메모리, 외부 통신 차단을 기록한다. 측정하지 않은 항목은 미실행으로 표시한다.

## 4. 회사 자료로 실험하기 전

- 사내 Windows PC에서 승인된 서명 번들로 설치·복구·제거와 재부팅 검증을 수행한다. 합성 `verify-bundle → doctor → demo`가 모두 통과한 뒤 회사 승인으로 다음 단계에 진입한다.
- 실제 문서, OCR 결과, 도면 이미지와 좌표, 질문, 추출 데이터는 승인된 회사 저장소에서만 취급한다. GitHub와 외부 로그에는 코드, 합성 자료, 반출을 승인받은 비내용 지표만 저장한다.
- PDF·Word·PPT 본문 및 내장 표·이미지, 스캔 이미지와 래스터 차트의 실처리 어댑터는 추가 작업이다. Track C가 받는 입력은 출처와 위치가 연결된 근거이며, 이 파일을 자동으로 읽어 주지는 않는다. Track C 합성 검증 후 형식별 파서를 연결해 원본 페이지·슬라이드·셀·이미지 영역을 검증한다. 실제 OCR 및 모델은 회사에서 허용한 CPU 실행 방식, 라이선스와 반입 승인 범위에서 선택한다. Microsoft Copilot도 회사가 승인한 서비스와 문서 등급 안에서만 사용한다.

## 이어받는 작업자에게 보낼 요청

> `feature/track-c-integration`에서 `docs/operator/CONTINUE-ON-COMPANY-PC.md`를 읽고 작업을 이어서 진행해. Track C 작업 5의 마지막 보완을 리뷰하고 테스트한 뒤 계획의 작업 6~9를 순서대로 완료해. 먼저 합성 자료로 검증한 뒤, 승인된 회사 Windows CPU 환경에서만 실제 문서를 실험해. 테스트 결과와 미검증 조건을 구분해 문서에 반영하고, 완료분을 같은 브랜치에 커밋·푸시해.
