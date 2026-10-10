# Bằng chứng kiểm thử

Các log này được tạo trong lượt hợp nhất, không phải lượt model thật. Baseline dùng source riêng D/P/K. Suite merge dùng Python 3.12, pytest-xdist 4 worker; phiên bản dependency ở ../requirements-tested.txt.

`offline_guard.py` là nội dung sitecustomize được nạp qua môi trường kiểm thử, chặn kết nối socket TCP/IP. Muốn tái lập, dùng venv riêng, cài dependency/dev extras và source checkout, đặt bản sao file này tên sitecustomize.py vào site-packages của venv (chỉ dành cho venv test). Chạy `python -m pytest -n4 --tb=short` tại repo root. Không chạy dsh/model thật để tái lập unit tests.

Dry run: `python scripts/run_cases.py --help` để xem lựa chọn; dùng fake_dsh theo README và chỉ định --level B0 đến B4. Token trong log là fixture, không phải usage thực. 16/16 là đúng kỳ vọng, bao gồm case cố ý ERROR.

JUnit có đường dẫn môi trường tại thời điểm chạy. Báo cáo chính giải thích xfail và các giới hạn.
