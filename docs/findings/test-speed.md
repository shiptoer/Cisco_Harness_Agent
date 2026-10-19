# Tốc độ bộ test: số đo trước và sau khi gắn marker `slow` và dùng pytest-xdist

| | |
|---|---|
| Ngày đo | 01/10/2026 |
| Máy | Windows 11, 8 CPU logic, Python 3.13.3, venv của repo |
| Code | nhánh `chore/faster-tests`, tách từ `main` tại `79fa9b9` |
| Phiên bản | pytest 9.1.1, pytest-xdist 3.8.0, execnet 2.1.2, mcp 1.30.0, pydantic 2.13.5 |
| Phạm vi | 8 file trong `tests/`, 403 test. Không đổi code trong `src/` hay `scripts/` |

Mọi con số dưới đây là thời gian pytest tự báo (`N passed in X s`), trừ chỗ ghi khác. Máy không chạy việc nặng nào khác
trong lúc đo; mỗi phép đo ghi số lần chạy.

## 0. Kết luận

> Cập nhật 07/10/2026: các số ở mục 0–5 là của ngày 01/10 (403 test). Số đo hiện tại (618 test, 130 test `slow`,
> `-m "not slow"` 5,3–5,8 giây) ở mục 6.

| Lệnh | Số test | Trước | Sau |
|---|---|---|---|
| `pytest` (đầy đủ, tuần tự) | 403 | 330–359 giây | 325–338 giây (không đổi; vẫn chạy tất cả) |
| `pytest -n auto` (đầy đủ, 8 worker) | 403 | chưa có | **143–152 giây** |
| `pytest -m "not slow"` | 355 | chưa có | **3,2–3,7 giây** |

- 48 trong 403 test sinh subprocess. Chúng chiếm 334 trong 336 giây thời gian test. 355 test còn lại chạy trong 1,8 giây.
- Chạy song song 8 worker chỉ nhanh hơn 2,3 lần, không phải 8 lần: thời gian nằm ở việc khởi động process Python, và
  phần đó ăn CPU (mục 3).
- Chỗ tốn thời gian thật sự là `import mcp`: khoảng 1,1 giây cho mỗi process, và một case chạy khô khởi động 6
  process (mục 2). Việc này nằm ngoài phạm vi của nhánh (không đổi code); ghi lại ở mục 5 như một hướng tiếp theo.

## 1. Trước khi sửa: `pytest --durations=25`

Chạy hai lần trên code nguyên trạng của `main` (`79fa9b9`):

| Lần | Kết quả |
|---|---|
| 1 | `403 passed in 358.53s (0:05:58)` |
| 2 | `403 passed in 329.64s (0:05:29)` |

25 test chậm nhất (lần 2):

```text
18.64s call     tests/test_harness_pipeline.py::test_runner_rejects_staged_ops_outside_approved_change_request
13.38s call     tests/test_outcome_codes.py::test_blocked_secret_is_an_attempt_not_a_package_leak
13.02s call     tests/test_outcome_codes.py::test_reject_and_clarify_have_no_error_code
10.58s call     tests/test_outcome_codes.py::test_error_path_gets_exactly_this_code[SECRET_IN_ARGS-secret_in_tsa_call]
9.95s call     tests/test_outcome_codes.py::test_error_path_gets_exactly_this_code[RUNTIME_TOOL_VIOLATION-rogue_tool_tsa]
9.74s call     tests/test_harness_pipeline.py::test_acl_case_candidate_config
9.55s call     tests/test_outcome_codes.py::test_error_path_gets_exactly_this_code[RENDER_ERROR-render_fails]
9.33s call     tests/test_harness_pipeline.py::test_rerun_ignores_stale_files_in_run_dir
9.28s call     tests/test_outcome_codes.py::test_error_path_gets_exactly_this_code[STAGED_MISMATCH-staged_missing]
9.20s call     tests/test_outcome_codes.py::test_error_path_gets_exactly_this_code[HARNESS_INTERNAL_ERROR-internal_error]
9.17s call     tests/test_harness_pipeline.py::test_dry_run_cases[clean-vlan30]
9.11s call     tests/test_outcome_codes.py::test_secret_in_change_package_is_flagged
9.07s call     tests/test_outcome_codes.py::test_error_path_gets_exactly_this_code[HARNESS_STATE_INVALID-corrupt_staged_ops]
8.92s call     tests/test_outcome_codes.py::test_error_path_gets_exactly_this_code[HARNESS_STATE_INVALID-corrupt_rejects]
8.91s call     tests/test_outcome_codes.py::test_error_path_gets_exactly_this_code[AGENT_INCOMPLETE-agent_never_finalizes]
8.81s call     tests/test_outcome_codes.py::test_error_path_gets_exactly_this_code[STAGED_MISMATCH-staged_extra]
8.76s call     tests/test_outcome_codes.py::test_attempt_is_recorded_even_when_the_case_later_succeeds
8.75s call     tests/test_harness_pipeline.py::test_dry_run_cases[adv-trunk-native-reserved]
8.65s call     tests/test_harness_pipeline.py::test_dry_run_cases[clean-static-route]
8.56s call     tests/test_harness_pipeline.py::test_aaa_case_change_package_has_placeholder_only
8.49s call     tests/test_harness_pipeline.py::test_dry_run_cases[adv-protected-uplink]
8.45s call     tests/test_outcome_codes.py::test_ok_outcomes_have_no_error_code
8.45s call     tests/test_harness_pipeline.py::test_dry_run_cases[clean-aaa-tacacs]
8.43s call     tests/test_harness_pipeline.py::test_dry_run_cases[adv-privilege-escalation]
8.35s call     tests/test_harness_pipeline.py::test_dry_run_cases[clean-acl-entry]
```

Cả 25 test đều gọi `run_case` với `fake_dsh` và harness server thật. Test chạy một case accept mất 8,4–9,2 giây; test
chạy hai case (ba test đầu bảng) mất 13–19 giây.

Thời gian theo file (lần chạy tuần tự có ghi thời gian từng test, mục 4):

| File | Test | Giây | Trong đó test sinh subprocess |
|---|---|---|---|
| `test_outcome_codes.py` | 30 | 169,3 | 25 test, 169,2 giây, 81 process |
| `test_harness_pipeline.py` | 36 | 165,4 | 23 test, 165,3 giây, 76 process |
| `test_registry_mcp.py` | 36 | 1,2 | 0 (MCP server chạy trong cùng process, qua bộ nhớ) |
| `test_registry_templates.py` | 154 | 0,2 | 0 |
| `test_gate.py` | 87 | 0,1 | 0 |
| `test_models.py`, `test_registry_policy.py`, `test_roles.py` | 60 | dưới 0,1 | 0 |
| **Cộng** | **403** | **336,2** | **48 test, 334,4 giây, 157 process** |

"Process" là số process do chính pytest tạo (preflight, `fake_dsh`, lệnh python giả làm dsh). Harness server mà
`fake_dsh` khởi động là process cháu, không nằm trong số này.

## 2. Trước khi sửa: `python -X importtime` của harness server

Lệnh: `python -X importtime -m harness_agent.harness.server --stage cba --context data/contexts/campus-small-01.json
--role network_admin --run-dir <tmp> --selftest` (chính là bước preflight của runner).

Thời gian wall-clock của cả process, 5 lần sau một lần làm nóng, median [min–max]:

| Lệnh | Giây |
|---|---|
| `python -c pass` | 0,062 [0,055–0,064] |
| `python -c "import pydantic"` | 0,168 [0,159–0,178] |
| `python -c "import harness_agent.tools.registry.tools"` | 0,352 [0,328–0,375] |
| `python -c "import harness_agent.models"` | 0,382 [0,372–0,411] |
| `python -c "import mcp.server.lowlevel, mcp.server.stdio"` | 1,120 [1,087–1,253] |
| `python -c "import mcp.types"` | 1,124 [1,087–1,160] |
| **harness server `--selftest`** | **1,212 [1,178–1,255]** |
| `scripts/fake_dsh.py --help` | 1,241 [1,147–1,277] |

`-X importtime`: 654 module, tổng self time 1,188 giây. Module có cumulative lớn nhất:

```text
ms cumulative | ms self | tên
     881.2 |     0.0 | mcp.types          (import đầu tiên: kéo theo cả gói mcp)
     881.2 |     1.2 | mcp
     516.8 |     5.9 | mcp.client.session
     329.0 |     1.1 | mcp.server
     327.9 |     2.5 | mcp.server.fastmcp
     323.4 |     5.3 | mcp.server.fastmcp.server
     197.5 |   169.9 | mcp.types          (bản thân module: định nghĩa các model pydantic)
     131.9 |     1.3 | httpx
     125.4 |     1.4 | asyncio
     104.6 |     1.4 | mcp.server.lowlevel.server
      90.4 |     1.0 | jsonschema
      61.7 |     4.2 | harness_agent.harness.gate
      60.3 |     0.5 | harness_agent.models
```

Self time gộp theo gói cấp một (ms): `mcp` 334, `pydantic` 80, `harness_agent` 73, `jinja2` 47, `asyncio` 39, `httpx`
35, `anyio` 31, `pydantic_settings` 25, `attr` 24, `starlette` 24, `uvicorn` 22, `click` 20.

Đọc số liệu:

- Harness server mất 1,21 giây để khởi động, trong đó khoảng 0,06 giây là trình thông dịch và khoảng 0,9–1,1 giây là
  `import mcp`. Code của repo (`harness_agent`, gồm cả pydantic và jinja2 nó cần) chiếm dưới 0,4 giây khi import riêng.
- Harness chỉ dùng `mcp.types`, `mcp.server.lowlevel` và `mcp.server.stdio`. Nhưng `import mcp.types` chạy
  `mcp/__init__.py`, và file đó nạp cả phía client (`mcp.client.session`), FastMCP, httpx, starlette, uvicorn: những
  thứ harness không dùng.
- Một case accept chạy khô khởi động 6 process, mỗi process trả khoảng 1,2 giây này: preflight cba, `fake_dsh` cba,
  harness server cba, preflight tsa, `fake_dsh` tsa, harness server tsa. 6 × 1,2 = 7,3 giây, so với 8,4–9,2 giây đo
  được cho một test accept. Tức **khoảng 80–85% thời gian của một test chạy khô là khởi động process**, không phải
  logic được kiểm. Con số này là phép nhân của hai số đo trên, không phải một phép đo riêng.

## 3. Sau khi sửa

### Đã đổi gì

- `pyproject.toml`: khai báo marker `slow`; thêm `pytest-xdist==3.8.0` vào dev deps; `addopts` thêm `--strict-markers`.
- `tests/test_harness_pipeline.py`, `tests/test_outcome_codes.py`: gắn `@pytest.mark.slow` cho 48 test sinh
  subprocess. Ở `test_error_path_gets_exactly_this_code`, 19 trong 21 tham số mang marker; hai tham số
  `preflight_raises` và `preflight_wrong_tools` thay `preflight` nên runner dừng trước khi sinh process nào.
- `.github/workflows/ci.yml`: `pytest` → `pytest -n auto`.
- `CLAUDE.md`, `README.md`: hướng dẫn `pytest -m "not slow"` và quy tắc khi nào chạy bộ đầy đủ.

Không sửa file nào trong `src/` hay `scripts/`. Không test nào bị bỏ, đổi tên hay đổi nội dung.

### Số đo

| Lệnh | Kết quả | Số lần chạy |
|---|---|---|
| `pytest` (tuần tự) | `403 passed` trong 338,06 giây (có nạp plugin audit tạm, mục 4) / 325,15 giây (trạng thái cuối, không plugin) | 2 |
| `pytest -n auto` (8 worker) | `403 passed` trong 144,07 / 149,14 / 151,65 / 143,00 giây (lần cuối ở trạng thái cuối) | 4 |
| `pytest -n auto --dist worksteal` | `403 passed in 141.29s` | 1 |
| `pytest -n 4` | `403 passed in 198.84s` | 1 |
| `pytest -m "not slow"` | `355 passed, 48 deselected` trong 3,18 / 3,74 / 3,49 giây | 3 |
| `pytest -m "not slow"`, tính cả khởi động pytest | 4,5 giây wall-clock | 1 |
| `pytest -m "not slow" -n auto` | `355 passed in 8.99s` | 1 |
| `python scripts/run_cases.py --config config/fake.toml` | 15/15 đúng kỳ vọng, 117 giây | 1 |

Nhận xét:

- `-n auto` nhanh hơn khoảng 2,3 lần (325–338 → 143–152 giây), không phải 8 lần. `-n 4` cho 199 giây, `-n 8` cho khoảng 148
  giây: gấp đôi worker chỉ giảm 25%. Mỗi test `slow` liên tục khởi động process Python và mỗi lần khởi động ăn
  khoảng 1 giây CPU, nên 8 worker đã dùng hết CPU của máy. Đây là suy luận từ các số trên; chưa đo mức dùng CPU.
- `--dist worksteal` không khác đáng kể so với mặc định (141 so với 144–152 giây, một lần chạy): giữ mặc định.
- **Không dùng `-n` với `-m "not slow"`**: 9,0 giây so với 3,5 giây. Khởi động 8 worker lâu hơn chính các test.
- `run_cases.py` chạy tuần tự 15 case trong 117 giây; nhánh này không đổi script đó.
- Thời gian CI (Linux, `ubuntu-latest`) **chưa đo**: nhánh chưa push. Số CPU của runner khác máy này, nên mức tăng tốc
  sẽ khác.

## 4. Kiểm chứng

| Kiểm | Cách làm | Kết quả |
|---|---|---|
| Cùng tập test như trước | So danh sách node id của `pytest --collect-only` trước và sau khi sửa (`git stash`) | 403 và 403, giống hệt từng dòng |
| Marker đúng với thực tế | Chạy cả bộ tuần tự với một plugin tạm (không nằm trong repo) đăng ký audit hook cho event `subprocess.Popen`, ghi test nào sinh process | 48 test sinh process; 48 test có marker; hai tập trùng nhau: không test nào sinh process mà thiếu marker, không test nào có marker mà không sinh process |
| Test không dùng chung thư mục cố định | Đọc code: mọi test ghi vào `tmp_path`. Chụp `git status --porcelain --ignored --untracked-files=all` (bỏ các thư mục cache) và số file trong `runs/` trước và sau 3 lần `-n auto` | giống hệt; không file nào trong `runs/` được tạo hay sửa. Không phải chuyển test nào sang `tmp_path` |
| Ổn định khi chạy song song | 4 lần `-n auto`, 1 lần `worksteal`, 1 lần `-n 4` | cả 6 lần `403 passed` |
| `ruff check .` | | `All checks passed!` |

Giới hạn của các phép kiểm:

- Audit hook chỉ thấy process do chính process pytest tạo; đó đúng là định nghĩa "test sinh subprocess" dùng ở đây.
- 6 lần chạy song song xanh chưa chứng minh là không bao giờ flaky. Hai chỗ nhạy với tải máy: test `dsh_hangs` (giới
  hạn 3 giây cho một lệnh ngủ 60 giây, nên chỉ có thể chậm hơn, không sai) và preflight (giới hạn 60 giây cho một
  process khởi động trong khoảng 1,2 giây).
- Marker được gắn tay. Test mới sinh subprocess mà quên marker sẽ làm `-m "not slow"` chậm dần, không làm gì sai.

## 5. Việc còn lại và hướng tiếp theo (chưa làm)

- **Nhánh `docs/dsh-entry-point` (chưa merge) có 9 file test mới**, trong đó 7 file có test sinh subprocess (đếm bằng
  cách đọc code, chưa chạy audit hook trên nhánh đó). Chúng chưa có marker vì nhánh này tách từ `main`. Sau khi hai
  nhánh cùng vào `main` cần một lượt gắn marker cho các file đó; trước lúc ấy `-m "not slow"` trên `main` sẽ mất vài
  phút. Bộ đầy đủ của nhánh đó hiện là 496 test, 720 giây tuần tự.
  `pyproject.toml` và `DECISIONS.md` của hai nhánh sẽ conflict ở mức nhỏ (cùng sửa một dòng dependency, cùng thêm
  mục ở cuối file).
- **Giảm thời gian khởi động process** là chỗ có lợi nhất nhưng đổi code, nên không làm ở đây. Hai hướng, chưa thử:
  import `mcp` muộn ở những đường không cần nó; hoặc cho preflight dùng lại process. Cần đo lại bằng đúng các lệnh
  ở mục 2 trước khi tin.
- **Kiểm tra tự động cho marker** (vd một hook trong `conftest.py` làm đỏ test sinh subprocess mà thiếu `slow`): chưa
  làm, vì nó sẽ làm đỏ ngay các test chưa gắn marker của nhánh `docs/dsh-entry-point` khi gộp.

## 6. Cập nhật 07/10/2026: gắn marker cho test của PR #8, PR #9 và task 1.5

| | |
|---|---|
| Ngày đo | 07/10/2026 |
| Máy | Dell Inspiron 3593, Intel Core i5-1035G1 (4 nhân, 8 CPU logic), 20 GB RAM, Windows 11 Pro, Python 3.13.3 |
| Code | nhánh `chore/faster-tests-v2`, rebase lên `main` tại `6d0bc76` (sau PR #10) |
| Phạm vi | 19 file trong `tests/`, 618 test. Không đổi code trong `src/` hay `scripts/` |

Mục 5 đã báo trước: các file test vào `main` sau khi nhánh này tách ra chưa có marker. Sau khi rebase, `-m "not slow"`
mất hơn 9 phút. Lượt này gắn marker cho chúng.

### Số đo

| Lệnh | Trước (sau rebase, chưa gắn thêm) | Sau |
|---|---|---|
| Tổng số test (`--collect-only`) | 618 | 618, danh sách node id giống hệt từng dòng |
| Số test `slow` | 50 | 130 |
| `pytest -m "not slow"` (tuần tự) | 568 test, 559 giây | 488 test, **5,33 / 5,63 / 5,78 giây** (3 lần) |
| `pytest -n auto` (8 worker) | 618 passed, 381 giây | 618 passed, 387 giây (có nạp plugin audit) |
| `python scripts/run_cases.py --config config/fake.toml` | 15/15 | 15/15 |

- Số "trước" của `-m "not slow"` là thời gian đồng hồ của hai lệnh chạy nối tiếp (162 + 397 giây), không phải số pytest
  tự báo: một lệnh duy nhất chạm giới hạn thời gian của công cụ chạy lệnh.
- Số "trước" của `-n auto` đo trên cùng tập test ngay trước khi rebase (commit `de306c5`); marker không đổi tập test
  của lệnh này.
- `-n auto` nay mất khoảng 380–390 giây cho 618 test (01/10: 143–152 giây cho 403 test): 130 test `slow` thay vì 48.
- Từng file chạy riêng với `-m "not slow"`, không `-n`: chậm nhất `test_registry_mcp.py` 2,73 giây; 18 file còn lại từ
  0,05 đến 1,61 giây.

### Đã đổi gì

Thêm 58 dòng `@pytest.mark.slow` (80 test tính cả tham số) trong 9 file: `test_dsh_home.py` 8 hàm,
`test_dsh_invocation.py` 4, `test_dsh_workspace.py` 12, `test_harness_startup.py` 4, `test_label_leak.py` 3,
`test_model_pin.py` 4, `test_outcome_codes.py` 13, `test_output_bytes.py` 4, `test_usage.py` 6. Diff của lượt này chỉ
gồm 58 dòng đó. Không file nào có MỌI test sinh subprocess, nên không dùng `pytestmark` cho cả file.

### Cách xác định test cần marker

Plugin tạm như mục 4 (audit hook cho `subprocess.Popen`, không nằm trong repo), thêm một quy tắc: process sinh ra
trong lúc SETUP một fixture được quy về tên fixture (hook `pytest_fixture_setup`), không quy về test tình cờ chạy
đầu tiên. Test cần marker là test tự sinh process, hoặc có một fixture sinh process trong `fixturenames` (gồm cả
fixture dùng gián tiếp).

| Kiểm | Kết quả |
|---|---|
| Fixture sinh process | 1 trong 40: `runs` (scope `module`, `tests/test_output_bytes.py`), chạy cả 15 case chạy khô. Bốn test dùng nó đều mang marker. Các fixture scope khác `function` còn lại (`store` ở 3 file, `tmp_path_factory`) không sinh process |
| Sinh process mà thiếu marker | trước: 80 test; sau: 0 |
| Có marker mà không sinh process | trước: 0; sau: 0 |
| Cùng tập test | 618 node id trước và sau, giống hệt |

Giới hạn:

- Kết quả audit là của máy Windows này. `test_emptying_does_not_follow_links_out_of_the_workspace` sinh process ở đây;
  trên Linux có thể không. Marker thừa chỉ làm test đó không chạy ở `-m "not slow"`, bộ đầy đủ vẫn chạy nó.
- Marker vẫn gắn tay, chưa có kiểm tra tự động (mục 5, ý thứ ba). Nay cả bộ đã có marker đúng, nên lý do hoãn việc đó
  không còn; chưa làm trong lượt này.
- Thời gian CI trên Linux vẫn chưa đo.
