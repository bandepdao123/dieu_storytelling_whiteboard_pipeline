# Nghiệm thu đợt 2 — phạm vi được phê duyệt

Sếp xác nhận “Ok” trong thread Discord Workflow whiteboard storytelling sau đề nghị nghiệm thu có giới hạn checkpoint v3 và cập nhật GitHub. Đây là chấp thuận phạm vi dưới đây, không phải chứng nhận production toàn diện.

## Kiểm chứng trước khi phát hành

- Full regression do agent chính chạy trên candidate: **940 passed in 156.30s**.
- Review độc lập bằng cx/gpt-6-astra: **194 passed in 58.58s**; không phát hiện blocker kỹ thuật mới trong phạm vi targeted review.
- `git diff --check`: PASS.
- Baseline trước commit đợt 2: `929511ebcf42c68f2530e40e5b81fa1f58b543d8`.

Các nhóm đã kiểm chứng gồm transaction/savepoint rollback và migration cancellation cleanup; final-publication stale compensation; job admission/resume evidence gates; durable keyed command-copy; measured PCM WAV narration; CLI inspection/media/recovery và tài liệu schema v15. Các regression khác nằm trong full suite; không suy ra bảo đảm ngoài trường hợp đã kiểm tra.

## Giới hạn checkpoint v3 được chấp nhận

- Ordinary restore đối với scene-bearing v3 vẫn fail closed.
- OWNER có thể opt-in xác thực lại nội dung khi canonical scenes, inventory và bytes còn khớp các điều kiện được quy định.
- Recovery tạo generation mới; không khôi phục authorization/QA/approval cũ, không tự resume job, không cấp lại attempt budget.
- Không phục hồi historical audio/cues/documents thiếu hoặc đã thay đổi; không nhập bộ scene lịch sử khác thay cho plan hiện tại.
- Phục hồi lịch sử rộng hơn không thuộc đợt nghiệm thu này; cần đặc tả và phê duyệt riêng.

Chi tiết hợp đồng và hướng dẫn: [legacy-content-recovery.md](legacy-content-recovery.md).

## Ranh giới phát hành

Chỉ cập nhật mã nguồn, tests và tài liệu trên GitHub. Không triển khai production, sửa production DB, đổi provider/model tạo ảnh, retention/license/settings hoặc gọi dịch vụ tạo ảnh trả phí. Mặc định tạo ảnh vẫn Codex OAuth GPT Image 2 high, không tự fallback.

Các báo cáo/plans trước quyết định này giữ nguyên giá trị bằng chứng lịch sử; mục nói compatibility decision OPEN được thay thế bởi quyết định nghiệm thu có giới hạn tại tài liệu này, không phải bởi việc bổ sung khả năng phục hồi lịch sử toàn diện.
