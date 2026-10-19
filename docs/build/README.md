# docs/build

`md2docx.js` chuyển một file Markdown của `docs/` thành `.docx` (marked lexer → docx-js; có trang bìa, mục lục tĩnh,
mỗi mục `##` sang trang mới). Bản Markdown là bản gốc: sửa `docs/HUONG_DAN_SU_DUNG.md` rồi sinh lại bản Word bằng
`node docs/build/md2docx.js docs/HUONG_DAN_SU_DUNG.md docs/HUONG_DAN_SU_DUNG.docx` ở gốc repo. Cần Node 18+ và hai gói
npm `docx`, `marked` (cài toàn cục `npm install -g docx marked`, hoặc cục bộ trong `docs/build` bằng `npm install docx
marked`, không commit `node_modules`). Chữ trên trang bìa và header nằm ở các hằng `COVER_VERSION`, `COVER_STATUS`,
`HEADER_TEXT` đầu file: đổi khi phát hành bản mới. Kiểm file sinh ra: `python -c "import zipfile;
zipfile.ZipFile('docs/HUONG_DAN_SU_DUNG.docx').testzip()"` không in gì là file zip hợp lệ.
