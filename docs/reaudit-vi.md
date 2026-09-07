# Phản hồi Operational Re-audit — baseline d7ab6545

> Historical response at the named baseline, not current production acceptance.
> Preserve evidence below; current limitations are in capabilities.md and the
> 2026-09-06 Phase2 plan, including subsequent independently reproduced blockers.

## Phạm vi xác minh

- Tài liệu nguồn: `Dieu_Storytelling_Whiteboard_Pipeline_Operational_Reaudit_d7ab6545.pdf`.
- Baseline đối chiếu: `d7ab6545cb363bd49978a359ae515091a15dc568`.
- Mỗi finding được kiểm tra lại bằng code path và regression test; chỉ sửa finding tái hiện được.
- Kết luận quan trọng: **không có finding bắt buộc nào là false positive hoàn toàn**. Một số mục là product gap hoặc improvement, không phải runtime regression.

## Ma trận kết luận

| ID | Kết luận | Trạng thái sau xử lý |
|---|---|---|
| OP-01 — Scene approval/evidence | TRUE BUG | Đã sửa: approval SCENE gắn evidence hash; gate kiểm tra approval active và hash hiện tại; mutation theo scene không revoke nhầm scene khác. |
| OP-02 — Discord stale lease/idempotency | TRUE BUG | Đã sửa trong canonical core: token riêng mỗi claim, CAS completion, receipt với khóa `discord:<message_id>`, conflict fail-closed. Transport vẫn at-least-once. |
| OP-03 — Checkpoint restore | TRUE BUG | Đã sửa: snapshot v3 tự kiểm chứng; validate đầy đủ trước mutation; version luôn tăng; không hồi sinh lifecycle state đã dừng. |
| OP-04 — Worker runtime | PRODUCT GAP | Chưa triển khai; không vá giả. Cần worker claim/heartbeat/reclaim/terminal transitions và provider idempotency. |
| OP-05 — Pause/cancel/resume fencing | TRUE BUG | Đã sửa: transition project và job atomic; xóa lease; resume chỉ mở job PAUSED đúng current version. |
| OP-06 — Cleanup/reconciliation | TRUE BUG | Đã sửa: cleanup SUPERSEDED, nested reconciliation, shared URI safety và SQLite single-writer lease/fencing. |
| OP-07 — Sheet retry hot-loop | PRODUCT GAP | Chưa triển khai retry subsystem gồm backoff, attempt_count, dead-letter và fake-clock tests. |
| OP-08 — SQLite cross-thread | IMPROVEMENT/CONTRACT | Không bật `check_same_thread=False`; contract đúng là một connection mỗi thread/process. Cần hardening/benchmark riêng. |
| Payload/checkpoint/response lớn | IMPROVEMENT | Cần pagination/summary/slim snapshot theo thay đổi contract riêng. |
| Sheets full sync/Drive round trips/startup scan | IMPROVEMENT | Cần benchmark và tối ưu delta/cache riêng. |
| Provider/token accounting | PRODUCT GAP | Repo chưa có provider production; không suy diễn usage/billing và không thêm provider giả. |
| CI/lock/license | PRODUCT GAP/GOVERNANCE | Chưa thuộc runtime hotfix; cần release workstream riêng. |
| Ruff debt | IMPROVEMENT | Không mass-format trong hotfix để tránh che khuất diff logic. |

## Bằng chứng kỹ thuật chính

### Approval và evidence

- Approval SCENE lưu hash evidence xác định theo scene, QA và active artifact.
- Pilot gate yêu cầu approval row chưa revoke và hash khớp evidence hiện tại.
- Thay QA/artifact/decision chỉ invalidate đúng scene liên quan và downstream gate.

### Discord

- Mỗi claim/reclaim nhận token UUID riêng; owner cũ không thể commit response sau takeover.
- Canonical mutation và `command_receipts` được ghi cùng transaction.
- Reuse idempotency key với nội dung/role khác bị từ chối.
- Migration v7 chuyển legacy `PROCESSING` không xác định side effect sang `MANUAL_REVIEW`, xóa lease và không tự replay.
- Giới hạn: Discord transport/provider ngoài core vẫn at-least-once; không tuyên bố distributed exactly-once.

### Restore

- Snapshot v3 chứa canonical project config, exact scene set/order và artifact inventory/lineage/checksum.
- Digest, scene identity, artifact metadata và binary checksum được kiểm tra trước mutation.
- Restore thất bại không làm thay đổi DB; restore thành công tạo `current_version + 1`.
- Trạng thái lifecycle và `blocked_reason` hiện tại được giữ nguyên, nên snapshot ACTIVE không thể kích hoạt lại project PAUSED/BLOCKED/COMPLETED.

### Cleanup và reconciliation

- Cleanup và reconciler dùng SQLite single-writer lease với token riêng, expiry/reclaim, renew/release có ownership check.
- Lease được fence trước filesystem mutation; stale owner fail-closed.
- Hỗ trợ nested managed paths, không follow symlink/path escape.
- Shared URI legacy được xử lý theo tất cả reference rows; binary chỉ bị xóa khi không còn live reference.
- Nhiều `.deleting-*` được xử lý xác định và kiểm tra content trước restore/xóa.
- `scheduled_cleanup()` chạy reconciliation trước retention cleanup.
- Giới hạn đã chấp nhận: chỉ bảo đảm **single-host dùng chung một file SQLite**; không phải distributed lock/multi-host exactly-once.

## Verification cuối

- Full regression suite: **102 passed**.
- `python -m build`: PASS.
- Cài wheel trong virtual environment sạch và CLI/import smoke: PASS.
- `git diff --check`: PASS.
- Independent release review cuối: PASS, không còn security/logic blocker trong phạm vi single-host/shared-SQLite.

## Kết luận gửi tester

Audit hữu ích và các finding chính không phải lỗi ảo. Các TRUE BUG đã được sửa và khóa bằng regression test. Những mục chưa đóng được phân loại rõ là PRODUCT GAP hoặc IMPROVEMENT; không nên ghi nhận chúng là regression đã sửa, cũng không nên dùng để kết luận core hiện có provider/worker production.

Hệ thống sau bản sửa là prototype đã harden cho single-host, nhưng **chưa production-ready đầu-cuối** cho tới khi OP-04, provider idempotency và OP-07 được triển khai và nghiệm thu riêng.
