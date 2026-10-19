"""Collector (demo): thu thập network context từ thiết bị Cisco IOS thật của một lab, CHỈ ĐỌC (D-047).

- `fetch`: lấy output `show` qua SSH (Netmiko) hoặc từ thư mục đã lưu; allowlist lệnh, host theo inventory.
- `parse`: output `show` → dữ liệu của `NetworkContext`; giá trị secret bị xoá trước khi parse.
- `build`: trộn policy, ghi file context + `.meta.json`, chạy contract và invariants.
- `synth`: output `show` giả dựng từ cấu hình nền, cho chế độ offline và test.

Chạy: `python -m harness_agent.collector --help`.
"""
