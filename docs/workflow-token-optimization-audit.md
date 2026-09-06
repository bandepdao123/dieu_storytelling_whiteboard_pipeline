# Audit tối ưu workflow và token/chi phí AI

## Executive summary

Audit tại baseline `be5f499d4ff7b1a28aa456b267840d30b3e5f57d` (2026-09-06). Core là bộ điều phối SQLite/local; **không có lời gọi LLM/provider AI live**. Vì vậy token thực đo trong baseline và sau sửa là **0**; không có cơ sở để tuyên bố phần trăm tiết kiệm token hay tiền.

Một sửa P1 nhỏ, tương thích ngược đã được thực hiện theo RED→GREEN: thêm status summary gọn, không chứa danh sách scene, thay vì buộc consumer nhận toàn bộ scene. API `status` cũ không đổi. No-op re-plan đã được thử nhưng full regression chứng minh xung đột invariant chủ đích (re-plan phải fence job và lineage), nên thay đổi đó đã được hoàn tác.

## Workflow thực tế

`init/input document + audio + SRT` → `plan_scenes` → QA ảnh và approval S001–S005/special → batch image job (scheduler bounded, tối đa 3 attempt/scene) → post-batch QA + contact sheet + approval theo evidence → animation job → Final Assembly (hash, snapshot, FFmpeg/ffprobe, publication journal) → Sheets/Drive/Discord adapters → cleanup/reconciliation có lease.

Các đường thay đổi canonical fence job, revoke approval phù hợp, tăng project version và supersede lineage. Dry-run integration/assembly không ghi canonical state; Final Assembly publication có recovery journal và atomic no-replace.

## Token truth và contract provider tương lai

Hiện không có network model invocation, tokenizer, prompt runner hay provider billing receipt; tên image provider chỉ là metadata. Token usage thực đo = 0. `generation_request` chỉ dựng dictionary và chưa gửi mạng.

Provider contract roadmap (không triển khai giả):

* request key content-addressed từ canonical JSON của `template_id/version`, normalized prompt, model, provider, config và input evidence hash;
* cache receipt bất biến ghi request key, cache hit/miss, response/evidence hash;
* usage receipt ghi input/output tokens **do provider trả về**, estimated/actual cost tách biệt, currency, latency, retry class/attempt;
* budget theo project/stage với reserve/settle atomically; từ chối trước dispatch nếu vượt budget;
* batch payload chỉ chứa context chung một lần và delta scene tối thiểu; không dùng `status()`/checkpoint full snapshot làm model context;
* không ước lượng token nếu chưa chọn tokenizer/model; không ghi estimated thành actual.

## Findings

| Mức | Loại | Evidence | Impact | Effort | Recommendation |
|---|---|---|---|---|---|
| P2 | IMPROVEMENT | `plan_scenes` luôn `_invalidate`, xóa/chèn lại mọi scene khi plan không đổi | O(N) writes, nhưng hiện là invariant fence job/lineage được regression test bảo vệ | vừa/cao | chỉ xem xét no-op với contract explicit mới; không sửa |
| P1 | IMPROVEMENT | `status()` trả tất cả cột của mọi scene | payload tăng tuyến tính; 360 scene đo 151,271 bytes | thấp | `status_summary()`/CLI riêng, giữ API cũ (đã sửa) |
| P1 | PRODUCT GAP | không có live LLM, usage/cache/budget ledger | chưa thể kiểm soát token/cost khi nối provider | lớn | contract/receipt/budget roadmap ở trên |
| P2 | IMPROVEMENT | checkpoint v3 serialize project + toàn bộ scenes + artifacts | payload/storage O(N), nhưng restore phụ thuộc snapshot đầy đủ | vừa/cao | thiết kế checkpoint incremental mới có migration; không hotfix |
| P2 | TRUE ISSUE | assemble loop query approval + artifact và hash từng scene | N+1 DB query và I/O scan ở 50–360 scene | vừa, invariant cao | batch query nhưng cần E2E benchmark và race/evidence tests |
| P2 | IMPROVEMENT | manifest hash full scans/serialization, gọi nhiều lần trong publication | CPU/DB O(N), bảo vệ integrity | vừa/cao | cache chỉ với mutation receipt/version; không bỏ validation |
| P2 | IMPROVEMENT | cleanup/reconciliation scan filesystem và query từng URI group | tăng theo artifact/filesystem | vừa | bounded pagination + batched ownership lookup |
| P3 | PRODUCT GAP | job có lease fields nhưng không có production worker/provider | chưa có distributed backpressure/resume execution | subsystem lớn | roadmap, không fake worker/network |

## RED→GREEN và benchmark

RED lưu từ focused test đầu tiên:

* identical re-plan: expected version giữ `1`, thực tế thành `2`;
* `Pipeline.status_summary`: `AttributeError`.

GREEN: `tests/test_workflow_optimization.py`: **11 passed** (bao gồm 500 job-kind đối nghịch và kiểm tra version indicator).

Harness tái lập: `python scripts/benchmark_status_summary.py`. Mỗi kích thước tạo SQLite in-memory mới bằng public init/import/plan, 4 giây/scene; trace chỉ các câu `SELECT` trong lời gọi status; byte là JSON UTF-8 compact, sort-key. Không đo thời gian. Kết quả chính xác của harness:

| scene | status full | full SELECT | summary | summary SELECT |
|---:|---:|---:|---:|---:|
| 50 | 21,865 B | 3 | 702 B | 7 |
| 360 | 152,758 B | 3 | 706 B | 7 |

Summary có ceiling công khai 8 SELECT (harness hiện dùng 7 khi không cần `OTHER`, tối đa 8), không write và không trả danh sách scene/history/response tùy ý. Không cam kết hard byte cap. `current_jobs` giữ top 8 nhóm ổn định và tối đa một dòng `OTHER` với tổng job cùng `unknown_group_count`; kind/state bị cắt lần lượt 48/24 ký tự. Project name và blocked reason được cắt lần lượt 128/256 ký tự; indicator tối đa 8 dòng, chỉ lấy project version hiện tại, với code ngữ nghĩa `JOB_FAILED`/`JOB_BLOCKED`. Gate `current` nghĩa approval APPROVED, chưa revoke, đúng project version hiện tại và evidence digest khớp chính xác evidence hiện tại; pilot yêu cầu từng scene S001–S005/special có approval evidence tương ứng. API `status` đầy đủ cũ không đổi.

## Hướng dẫn operator

Dùng `du-pipeline --db pipeline.db status-summary PROJECT_ID` cho polling/dashboard/provider context. Chỉ dùng `status` khi cần chi tiết scene. Có thể gọi `plan` lặp lại an toàn nếu audio/SRT/special set không đổi; khi khác, invalidation/version/evidence vẫn hoạt động như trước.

## Limits và roadmap

Không triển khai provider/network/worker, tokenizer, cache giả hay giá giả. Chưa tối ưu Final Assembly N+1 vì vùng này có invariant approval/version/evidence, filesystem safety và publication lease; cần benchmark FFmpeg E2E riêng. Bước kế tiếp: schema usage receipt append-only, provider-neutral request normalizer, budget reservation, rồi adapter thật trả usage; checkpoint v4 incremental chỉ sau kế hoạch migration/restore compatibility.
