# Dieu Storytelling Whiteboard Pipeline — Phase 1

Nền tảng điều phối chạy được bằng SQLite cho pipeline kể chuyện vi/en. Phase 1 quản lý metadata, kiểm tra timing, trạng thái, retry, approval, lineage và audit; **không** tạo ảnh trả phí, hoạt họa, render MP4 hay tuyên bố precision whiteboard.

## Yêu cầu và cài đặt chính xác

```bash
cd /home/hermes/projects/dieu_storytelling_whiteboard_pipeline
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
# tests dùng pytest (dependency phát triển duy nhất)
python -m pip install pytest
python -m pytest -q
```

Runtime dùng hoàn toàn Python standard library. Python >=3.11.

## Chạy

```bash
du-pipeline --db demo.db init "Demo" --language vi --seed 42
# lấy project_id từ JSON, rồi:
du-pipeline --db demo.db import-audio PROJECT_ID narration.wav 60000 SHA256
du-pipeline --db demo.db import-srt PROJECT_ID narration.srt
du-pipeline --db demo.db plan PROJECT_ID
du-pipeline --db demo.db status PROJECT_ID
du-pipeline --db demo.db pause PROJECT_ID
du-pipeline --db demo.db resume PROJECT_ID
du-pipeline --db demo.db retry SCENE_DATABASE_ID
du-pipeline --db demo.db approve PROJECT_ID S001 --actor owner
du-pipeline --db demo.db reject PROJECT_ID S002 --actor reviewer
du-pipeline --db demo.db report PROJECT_ID
```

`import-script PROJECT_ID KIND SOURCE` nhận kind `text|docx|gdocs|excel|url` và chỉ lưu metadata chuẩn hóa ở Phase 1. SRT/audio có command riêng. Remote GDocs/URL, Excel/DOCX extraction are provider interfaces, không fetch mạng. Không đọc/lưu secret.

## Quy tắc chính

Audio là đồng hồ chuẩn. Cue rỗng, overlap, ngoài audio hoặc lệch điểm cuối >500ms sẽ BLOCKED, không tự căn chỉnh. S001–S005 và scene special cần duyệt. Image attempt tối đa 3; chỉ scene đó BLOCKED. Binary lỗi bị xóa ngay, metadata attempt còn; artifact thường có TTL 3 ngày. SQLite là source of truth; Sheet 11 tab và Drive folders là contract. Provider image/animation là Protocol plugin.

Metadata output mặc định mô tả 1920x1080, 30fps, 16:9 H.264 MP4; hard cut, không music/sub/logo, final hold 1–2s nằm trong duration. Core local đã có dispatcher `du-*` không dấu với schema/RBAC, checkpoint stage, restore latest, rerun có approval + invalidation/version, duration exception và cleanup idempotent bảo vệ dữ liệu bất biến. Render thực tế, contact-sheet image synthesis, AI-QA model invocation, cloud/Discord network adapters và ultimate precision **chưa live**, thuộc phase sau.
