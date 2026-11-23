# Secure Multimodal RAG Lab

보안 문서의 표, 차트, 설계도면, 이미지와 다국어 텍스트를 대상으로 Microsoft 허용 경로와 CPU 전용 로컬 경로를 비교하는 실험 프로젝트입니다.

이 저장소에는 회사 문서, 문서에서 추출한 텍스트·이미지·임베딩, 실제 질문·답변, 모델 가중치 또는 내부 시스템 정보가 들어가면 안 됩니다. 실제 실험은 승인된 회사 환경에서만 수행합니다.

현재 단계는 승인용 설계 검토입니다.

- 기준 명세와 gstack/Superpowers 검토 결과: [`docs/superpowers/specs/2026-11-23-secure-multimodal-rag-design.md`](docs/superpowers/specs/2026-11-23-secure-multimodal-rag-design.md)
- 필수 엔지니어링 시험 계획: [`docs/reviews/2026-11-24-engineering-test-plan.md`](docs/reviews/2026-11-24-engineering-test-plan.md)
- 승인 후 상세 구현계획 색인: [`docs/superpowers/plans/README.md`](docs/superpowers/plans/README.md)

현재 권고는 상세 구현계획 작성에 대한 `CONDITIONAL GO`, 실제 보안문서 실행에 대한 `NO-GO`입니다. 실제 문서는 회사 승인, 오프라인 반입, 사전 진단, 보안 시험을 통과한 뒤에만 사용합니다.

