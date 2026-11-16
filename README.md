# WinFinder

Tìm **store / sản phẩm / angle đang vào phase scale**, tức là đang tăng thật chứ không phải đã bão hòa, dựa trên 4 trụ:

| # | Trụ | Tín hiệu | Nguồn | Trạng thái |
|---|-----|----------|-------|-----------|
| 1 | **Ad growth** | Creative mới/tuần tăng đều 3–6 tuần, store/domain < 90 ngày | Meta Ad Library API, RDAP, crt.sh, Shopify `products.json` | ✅ v0.1 |
| 2 | **Traffic** | Traffic tăng song song với ad | CrUX popularity rank (BigQuery), EU reach, branded search | ⏳ |
| 3 | **Trends** | Keyword set → xu hướng 5 năm, tách xu hướng thật khỏi mùa vụ | Google Trends, Autocomplete | ⏳ |
| 4 | **Thị trường** | Đã lan sang các nước EU nào | Ad Library theo từng nước | ✅ cơ bản (cột "Thị trường") |
| + | **Angle / persona** | Gom ad theo angle, đo tăng trưởng theo angle | Text ad + LLM | ⏳ |

> **Giới hạn dữ liệu:** Ad Library API chỉ trả quảng cáo thương mại **phân phối ở EU**. Ad chỉ chạy ở US thì không lấy được qua API. Store US đã scale thường chạy cả IE, NL, DK, SE (nơi ad tiếng Anh phổ biến), nên tool dùng các nước này làm proxy.

## Cách chấm điểm (0–100)

Ad được gom theo **domain** (lấy từ link caption), nếu không có domain thì gom theo page.

- **velocity:** độ dốc số creative mới/tuần trong cửa sổ, đã chuẩn hóa theo trung bình.
- **consistency:** tỉ lệ số tuần có creative mới.
- **reach:** tổng `eu_total_reach`, tính theo thang log.
- **newness:** tuổi store. Ưu tiên ngày đăng ký domain (RDAP), sau đó đến ngày cấp SSL đầu tiên (crt.sh), cuối cùng là ngày ad đầu tiên (chỉ là cận dưới, hiển thị dạng `≥`).
- **spread:** số nước EU store đã chạy.

Nhãn **SCALE** được gắn khi thỏa đủ: tổng số ad ≥ `min_total_ads`, số tuần có ad ≥ `min_active_weeks`, nửa sau cửa sổ / nửa đầu ≥ `min_growth_ratio`, độ dốc > 0 và 2 tuần gần nhất vẫn còn ra creative. Mọi ngưỡng và trọng số nằm trong `config.yaml`.

## Chạy

```bash
pip install -e ".[dev]"

winfinder demo                      # dữ liệu giả lập, không cần token → demo_out/index.html
pytest -q

export META_AD_LIBRARY_TOKEN=...    # token Ad Library API
winfinder run                       # collect → enrich → analyze → reports/index.html
winfinder analyze --enrich          # chấm lại từ DB hiện có
```

## Chạy tự động trên GitHub Actions

1. Tạo token: vào developers.facebook.com, tạo app, hoàn tất xác minh danh tính, rồi lấy access token có quyền truy cập Ad Library API.
2. Lưu token vào repo: **Settings → Secrets and variables → Actions → New secret**, đặt tên `META_AD_LIBRARY_TOKEN`.
3. Workflow **Weekly scan** chạy mỗi thứ Hai (hoặc bấm *Run workflow* để chạy tay). Lịch sử được lưu trong `data/winfinder.db`, báo cáo trong `reports/`, và cả hai được commit ngược lại vào repo.

Lưu ý: chạy hằng tuần mới tích lũy được lịch sử reach để đo tốc độ tăng thật.

## Cấu trúc

```
winfinder/
  sources/adlibrary.py   Meta Ad Library client (phân trang, xử lý rate limit)
  sources/domain.py      RDAP, crt.sh, Shopify products.json
  analysis/velocity.py   creative mới/tuần + nhận diện phase scale
  analysis/scoring.py    gom theo store, chấm điểm
  pipeline.py            collect / enrich
  report.py              xuất JSON + HTML
  demo.py                dữ liệu giả lập
  cli.py
```
