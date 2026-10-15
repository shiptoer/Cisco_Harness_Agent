# Cisco Harness Agent — bản hợp nhất D + P + K

Bản tích hợp riêng, không ghi đè ba nguồn. Base: **D**; thêm ablation/sensors/evaluator của **P** và các sửa lỗi, confirmed intent, prepare/review của **K**. Xem `MERGE_REPORT.md` và `MERGE_MANIFEST.json` để biết nguồn gốc và giới hạn kiểm thử.

## Chạy offline trên Windows

Mở terminal tại thư mục này, dùng Python >= 3.11:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -n 4
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\python.exe scripts\run_cases.py --config config\fake.toml --level B4
```

`config/fake.toml` dùng `fake_dsh.py`, không cần Node, dsh thật hay API key. Token hiển thị trong lượt chạy fake là số mô phỏng để kiểm tra bộ thu thập usage, **không phải chi phí hay kết quả model thật**. `requirements-tested.txt` ghi phiên bản môi trường đã kiểm thử; chưa kiểm lại trên Windows.

Các mức khác: thay `--level B4` bằng B0, B1, B2, B3. Đầu ra đặt dưới `runs/`; không deploy xuống thiết bị. Chạy nhanh phần không tạo subprocess: `python -m pytest -m "not slow"`.

## Kiến trúc hợp nhất

| Mức | Đường sinh CLI | Context invariants/projection | Gate | Sensors |
| --- | --- | --- | --- | --- |
| B0 | Text model → parser B0 | Không; context thô vào prompt | Không MCP | Shadow |
| B1 | CBA → ChangeRequest → TSA → registry/Jinja2 | Không; cấu trúc context vẫn kiểm | G1–G2; G3–G6 shadow | Shadow |
| B2 | Như B1 | Có | G1–G2; G3–G6 shadow | Shadow |
| B3 | Như B1 | Có | G1–G6 | Shadow |
| B4 | Như B1 | Có | G1–G6 | Syntax + CIS cưỡng chế; Batfish skipped |

Runner giữ model pin, workspace tách biệt, trace, kiểm tra trạng thái và quét leak từ D. B1–B4 chỉ render từ trạng thái do MCP server ghi; không suy thao tác từ câu trả lời của model. B0 là ngoại lệ có chủ đích của phép đối chứng.

**Lưu ý đo lường B0/B1:** context thô có thể chứa canary. `leak_sources` và `leak_attempt_detected` giữ cách đo rộng của D, bao gồm prompt/session do runner chủ động đưa vào. Không dùng riêng cờ này làm tỷ lệ tấn công thành công hoặc so sánh leak đầu ra giữa các mức. `leak_in_package` và evaluator candidate là phép đo đầu ra. Chỉ dùng inventory giả/canary cho các baseline này.

## Contract và quyền

- Contract giữ thiết kế D: file context `schema_version="1.0"` (khác số phiên bản contract v0.2); `trunk_allowed`, `banner_motd`, `reserved_vlans`; secret dùng `SecretStr` trong model.
- Nhãn corpus dùng `context_ref` (đường dẫn tương đối + SHA-256), không nhúng context. Generator của P đã chuyển sang contract này; context riêng nằm trong `contexts/` của corpus.
- Các file context cũ 0.1/0.2 của P không tự động được đoán rồi chuyển tại runtime. Corpus đã sinh bằng bản P cũ cần sinh lại hoặc migration được kiểm tra riêng.

| Role canonical | Bậc registry |
| --- | --- |
| viewer | 0 |
| access_operator | 1 |
| network_admin | 2 |
| security_admin | 3 |

`network_engineer` của K không phải alias runtime: khi chuyển case K, người chuyển phải đổi rõ sang `network_admin`; `network_admin` **cũ của K/P** tương đương `security_admin` mới. Không nhận ngầm role lạ và không nâng quyền tự động.

## Prepare / xác nhận / chạy

```powershell
python -m harness_agent.runner --config config/fake.toml --case data/cases/clean-vlan30.json --prepare-only
```

Prepare chỉ chạy CBA; kết quả `status=pending_review`, `verdict=null`, có `review.json` và `change_request.json`; chưa có staged ops hoặc CLI. `review.json` **không phải** hợp đồng phê duyệt. Người vận hành phải đọc đề xuất và tự tạo file xác nhận tin cậy với các trường:

```json
{
  "intent": "Ý định được xác nhận",
  "context_sha256": "<SHA-256 64 ký tự hex của snapshot>",
  "hash_mode": "raw",
  "changes": [{"tool": "create_vlan", "args": {"device": "SW2", "vlan_id": 30, "name": "FINANCE"}}]
}
```

Chạy lại với `--confirmed-intent <file>` để kiểm danh sách đề xuất trước TSA. Không kết hợp cờ này với `--prepare-only`. Không có API tự ký/duyệt đề xuất của model.

`hash_mode` mặc định `raw` để giữ hợp đồng K cũ. Tùy chọn `canonical-json-v1` dùng JSON sắp key, UTF-8, không whitespace dư; ý định chuẩn hóa Unicode NFC và khoảng trắng. Tính hash bằng `harness_agent.runner.intent_contract.context_digest`; thay đổi dữ liệu vẫn phải xác nhận lại. Đây là HITL tùy chọn, **chưa giải quyết grounding tự động**.

## Các công cụ nghiên cứu giữ lại từ P

- `scripts/gen_corpus.py`: generator đa thao tác deterministic; không được dùng để tiếp tục dự án dataset đó trong lần hợp nhất này.
- `scripts/run_corpus.py`: chạy corpus theo mức/lặp; `scripts/evaluate.py`: parse candidate CLI độc lập và chấm nhãn.
- `harness_agent.llm`: client/record/replay phục vụ tác vụ riêng, không thay dsh trong B0–B4; chỉ kiểm bằng transport giả, không gọi API trong lần kiểm thử này.
- `scripts/web_pipeline_server.py`: MCP stdio bridge tùy chọn, role lấy từ `NETWORK_ROLE` phía server (mặc định `access_operator`), vẫn bắt buộc `NETWORK_CONFIRMED_INTENT`; không phải web app đã deploy.

## Giới hạn đã biết

Grounding tự động còn một test xfail chủ đích ở pipeline: intent yêu cầu VLAN 20 nhưng fake CBA/TSA cùng chọn VLAN 10 và xuất cấu hình. Test truyền intent thật vào runner; G5 chỉ kiểm TSA khớp đề xuất CBA. Xem `docs/GROUNDING_TEST_FIX.md`; bản sửa test A9 không hiện thực grounding tự động. Syntax/CIS chỉ phủ phạm vi tool hiện có; Batfish chưa chạy, chưa xác thực bằng Cisco/EVE-NG. Không suy ra độ chính xác hay robustness LLM từ fake tests. Các hướng dẫn chạy model thật của D được giữ ở `docs/SETUP_WINDOWS.md`, nhưng chưa kiểm lại dsh thật sau merge. `config/model.toml` là nguồn ghim model duy nhất; không thêm model lần nữa vào base patch.

Bản ZIP không gồm venv, node_modules, API key, config máy riêng, bộ cài, run lịch sử hay dataset 2400. Log spike vốn có của D vẫn giữ nguyên. Tài liệu README/CLAUDE/DECISIONS gốc từng nguồn nằm trong `docs/archive/source-*`; những trạng thái tiến độ cũ không phải trạng thái bản hợp nhất.
