# WinFinder

Tìm **store / sản phẩm / angle đang vào phase scale**, tức là đang tăng thật chứ không phải đã bão hòa, dựa trên 4 trụ:

| # | Trụ | Tín hiệu | Nguồn | Trạng thái |
|---|-----|----------|-------|-----------|
| 1 | **Ad growth** | Creative mới/tuần tăng đều 3–6 tuần, store/domain < 90 ngày | Meta Ad Library API, RDAP, crt.sh, Shopify `products.json` | ✅ v0.1 |
| 2 | **Traffic** | Traffic tăng song song với ad | Lượt tìm tên brand (Trends), EU reach ✅ · CrUX rank ⏳ | ✅ một phần |
| 3 | **Trends** | Keyword set → xu hướng 5 năm, tách xu hướng thật khỏi mùa vụ | Google Trends, Autocomplete | ✅ v0.2 |
| 4 | **Thị trường** | Đã lan sang các nước EU nào | Ad Library + Trends theo từng nước | ✅ cơ bản |
| + | **Angle / persona** | Gom ad theo angle, đo tăng trưởng theo angle, gợi ý angle chưa ai khai thác | Text ad + Claude | ✅ |

> **Giới hạn dữ liệu:** Ad Library API chỉ trả quảng cáo thương mại **phân phối ở EU**. Ad chỉ chạy ở US thì không lấy được qua API. Store US đã scale thường chạy cả IE, NL, DK, SE (nơi ad tiếng Anh phổ biến), nên tool dùng các nước này làm proxy.

## Giao diện web: nhập ngách, bấm quét, xem báo cáo

```bash
pip install -e .
winfinder serve          # mở http://127.0.0.1:8765 trên trình duyệt
```

- **Ngách:** thêm, sửa, xoá, bật/tắt. Mỗi ngách gồm tên, các keyword tìm ad (keyword đầu là keyword chính cho Trends), ngôn ngữ ad và ghi chú. Danh sách lưu trong `niches.yaml`, GitHub Actions cũng đọc file này. Muốn workflow quét ngách mới thì commit `niches.yaml`.
- **Quét:** quét tất cả ngách đang bật, hoặc bấm "Quét" ở từng ngách. Chọn được bước cần chạy (Ad Library, Trends, Ứng viên, Angle). Log hiện trực tiếp. Bước nào thiếu key sẽ bị bỏ qua và ghi rõ lý do.
- **Báo cáo:** Ứng viên, Angle, Store, Trends mở ngay trên cùng trang.
- **API key:** nhập trên trang, lưu vào `.env` trên máy (quyền 600, đã gitignore, không hiển thị lại).

Chạy trên máy cá nhân còn có lợi là Google Trends ít bị chặn hơn so với chạy trên máy chủ GitHub.
Server chỉ nghe trên `127.0.0.1`. Mọi thao tác ghi cần token của phiên, và Host header được kiểm tra.
Quét từ dòng lệnh: `winfinder scan` (tất cả) hoặc `winfinder scan --niche "eye care"`.

## Cách chấm điểm (0–100)

Ad được gom theo **domain** (lấy từ link caption), nếu không có domain thì gom theo page.

- **velocity:** độ dốc số creative mới/tuần trong cửa sổ, đã chuẩn hóa theo trung bình.
- **consistency:** tỉ lệ số tuần có creative mới.
- **reach:** tổng `eu_total_reach`, tính theo thang log.
- **newness:** tuổi store. Ưu tiên ngày đăng ký domain (RDAP), sau đó đến ngày cấp SSL đầu tiên (crt.sh), cuối cùng là ngày ad đầu tiên (chỉ là cận dưới, hiển thị dạng `≥`).
- **spread:** số nước EU store đã chạy.

Nhãn **SCALE** được gắn khi thỏa đủ: tổng số ad ≥ `min_total_ads`, số tuần có ad ≥ `min_active_weeks`, nửa sau cửa sổ / nửa đầu ≥ `min_growth_ratio`, độ dốc > 0 và 2 tuần gần nhất vẫn còn ra creative. Mọi ngưỡng và trọng số nằm trong `config.yaml`.

## Trends: tăng bền vững hay chỉ là mùa vụ / nhất thời?

`winfinder trends` làm cho mỗi chủ đề (khai báo trong `config.yaml → trends.topics`):

1. **Bộ keyword:** seed + keyword thêm tay + gợi ý Google Autocomplete. Cache 30 ngày.
2. **Chuỗi 5 năm theo tuần cho từng thị trường.** Google Trends chuẩn hóa 0–100 riêng cho từng request và mỗi request tối đa 5 keyword, nên tool dùng `seeds[0]` làm **keyword neo** có mặt trong mọi request để quy tất cả về cùng một thang đo. Nhờ vậy cộng được các keyword lại thành chỉ số của cả chủ đề.
3. **Tách mùa vụ:** dùng trung bình trượt 2×52 tuần để lấy trend thô, rồi tính chỉ số mùa vụ theo từng tuần trong năm, sau đó trừ ra để được chuỗi đã khử mùa vụ. Cách này cho biết xu hướng tới tận tuần gần nhất mà không bị đỉnh mùa đánh lừa.
4. **Phân loại:**

| Nhãn | Điều kiện chính |
|---|---|
| Tăng bền vững | 13 tuần gần nhất ≥ +20% so với cùng kỳ năm trước **và** chuỗi khử mùa vụ dốc lên ≥ 10%/năm |
| Bùng nổ gần đây | 8 tuần gần nhất ≥ 2× mức nền, chưa đủ lịch sử để nói là bền. Cần theo dõi tiếp |
| Nhất thời (đã qua đỉnh) | Đỉnh trong 26 tuần ≥ 3× mức nền nhưng 4 tuần gần nhất đã rơi còn ≤ 50% đỉnh |
| Mùa vụ | Chu kỳ năm đều đặn (F_s ≥ 0.5) **và** biên độ ≥ 40% mức trung bình, không tăng qua các năm |
| Đang giảm | YoY ≤ −20% và xu hướng khử mùa vụ đi xuống |
| Ổn định / bão hòa | Còn lại |

5. **Lan rộng thị trường:** nước đầu tiên trong `geos` là thị trường gốc (US). Tool cho biết chủ đề mới tăng ở gốc (đi trước được), đã lan sang nước nào, hay gốc đã chững còn nước khác mới tăng. Nên khai báo `geo_keywords` bằng tiếng bản địa cho DE/FR/NL.
6. **TAM (ước lượng):** điền `volume_hint`, là search/tháng của `seeds[0]` lấy từ Google Keyword Planner. Tool quy đổi chỉ số Trends của cả bộ keyword ra search/tháng.

**Nguồn dữ liệu (`trends.provider`):**
- `serpapi`: ổn định và chạy được trên GitHub Actions. Cần secret `SERPAPI_API_KEY`.
- `direct`: gọi thẳng trends.google.com. Miễn phí nhưng không chính thức, và Google hay chặn IP máy chủ, nên hợp chạy trên máy cá nhân.
- `csv`: tải CSV bằng tay trên trends.google.com rồi bỏ vào `data/trends_csv/US/…csv`. Luôn dùng được.

```bash
winfinder trends                                   # mọi chủ đề trong config
winfinder trends --topic posture --force           # 1 chủ đề, bỏ qua cache
winfinder trends --keyword "heated vest, heated jacket" --geos US,GB,DE   # thử nhanh
winfinder trends --provider direct                 # chạy miễn phí từ máy cá nhân
```

Báo cáo nằm ở `reports/trends.html` và `reports/trends.json`.

## Ứng viên: nối store đang scale với nhu cầu thị trường

`winfinder link` lấy top store theo điểm ad, sau đó:

1. **Hiểu sản phẩm là gì**, kể cả khi tên chỉ là brand (vd "LiftEyes" thực ra là kính ánh sáng đỏ trị bọng mắt):
   - **Đọc trang sản phẩm:** chọn hero product trong Shopify `/products.json`, tức sản phẩm khớp nhiều từ nhất với ad (tính cả tên brand), rồi lấy mô tả, `product_type` và tags. Không phải Shopify, hoặc thiếu thông tin, thì tải trang sản phẩm/trang chủ và đọc `<title>`, meta description, JSON-LD Product, h1/h2 và phần chữ chính. Cache 14 ngày.
   - **Claude đọc ad + trang** (`link.llm`, cần `ANTHROPIC_API_KEY`) và trả về structured output gồm: tên gọi chung của sản phẩm, 2–6 keyword người mua tìm trên Google (không chứa brand), keyword nhu cầu/vấn đề (vd "eye bags"), tệp khách và độ tin cậy. Kết quả được cache theo hash nội dung, nên ad và trang không đổi thì không gọi lại. Request dùng `effort: low` và fallback phía server khi bị bộ lọc an toàn từ chối.
   - **Không có key → heuristic n-gram** trên ad + trang: `product_type` ×4, tên SP ×3, tag ×2, mô tả ×1; keyword neo lấy từ `product_type` nếu có; giữ đúng dạng viết người ta tìm ("eye bags", không phải "eye bag").
2. **Chạy Trends cho sản phẩm và cho nhu cầu.** Các store bán cùng sản phẩm dùng chung một lần gọi. Khi tên sản phẩm còn quá mới, ít người tìm, mà nhu cầu gốc (vd "eye bags") có dữ liệu, kết luận sẽ dựa trên nhu cầu gốc.
3. **Đếm đối thủ:** số store khác có ad nhắc tới keyword đó.
4. **Kết luận**, với điểm tổng = 55% điểm ad + 45% điểm trends:

| Kết luận | Khi nào |
|---|---|
| Ứng viên mạnh | Store có nhãn SCALE và nhu cầu đang tăng bền vững hoặc bùng nổ |
| Nhu cầu đang lên | Nhu cầu tăng nhưng store chưa scale rõ |
| Mùa vụ, đang vào mùa / ngoài mùa | Nhu cầu mùa vụ. Đang vào mùa thì là cơ hội ngắn hạn |
| Ít người tìm kiếm | Trends không có dữ liệu. Nhu cầu do ads tạo ra (sản phẩm impulse), nên dựa vào ad growth và angle |
| Bão hòa | Nhu cầu ổn định. Chỉ vào nếu có angle khác biệt |
| Rủi ro | Nhu cầu đang giảm hoặc đã qua đỉnh |

Nếu tool rút sai keyword, đặt tay trong `config.yaml → link.overrides`, vd `postureglow.com: ["posture corrector"]`.
Báo cáo nằm ở `reports/winners.html` và `reports/winners.json`.

## Traffic: ad tăng thì traffic có tăng song song không?

Chạy cùng `winfinder link` (`link.traffic`). Với mỗi store, tool so 12 tuần creative mới với:

- **Lượt tìm tên brand trên Google Trends** (12 tháng, theo tuần, toàn cầu). Tên brand lấy từ tên page, khớp với domain. Khi ad tăng, người ta search tên store, nghĩa là ad đang tạo ra khách thật.
- **Reach EU** của các ad ra mắt mỗi tuần.

| Kết luận traffic | Khi nào | Ảnh hưởng điểm tổng |
|---|---|---|
| Tăng cùng ad | Ad ≥ 1.5× và lượt tìm brand ≥ 1.3× (6 tuần gần / 6 tuần trước) | ×1.1 |
| Chưa tăng theo ad | Ad tăng, lượt tìm brand đứng yên. Có thể còn đang test, chưa chắc có lãi | ×0.9 |
| Tăng tự nhiên | Lượt tìm brand tăng dù ad không tăng | – |
| Đang giảm | Lượt tìm brand giảm ≥ 30% | ×0.9 |
| Chưa đủ lớn | Tên brand gần như chưa có lượt tìm (store nhỏ/mới, rất phổ biến với store dropship) | – |
| Tên mơ hồ | Lượt tìm đã có từ trước khi store chạy ad, tức tên trùng từ thông dụng, nên không đo được | – |

**Chưa làm:** hạng phổ biến Chrome (CrUX, BigQuery theo tháng). Phần này cần tài khoản Google Cloud và cần kiểm tra đúng cấu trúc bảng trước khi viết.

## Angle / persona: sản phẩm đang thắng nhờ angle nào?

Một sản phẩm thường được bán bằng nhiều angle, mỗi angle nhắm một tệp khách khác nhau (vd posture corrector: "đau lưng dân văn phòng", "dáng đẹp, tự tin", "quà tặng cho mẹ"). `winfinder angles` đọc `reports/winners.json` rồi làm như sau với từng sản phẩm, gộp mọi store cùng bán:

1. **Gom nội dung ad** (tiêu đề + nội dung) và bỏ trùng. Tối đa 150 nội dung/sản phẩm, ưu tiên nội dung chạy nhiều ad và mới nhất.
2. **Claude chia 3–8 angle**, gồm tên angle, persona, nỗi đau/mong muốn, lời hứa và kiểu hook (problem-solution, testimonial, gift, before-after…). Claude gán từng ad vào angle và **đề xuất 2–4 angle chưa ai khai thác**. Phần này dùng structured output, `effort: medium` và stream, vì output dài.
3. **Đo từng angle** bằng ngày bắt đầu chạy của ad, theo cùng cách đo creative velocity:

| Trạng thái | Nghĩa |
|---|---|
| Đang lên | Nửa sau cửa sổ có ≥ 1.5× creative mới so với nửa đầu. Các store đang đổ tiền vào angle này |
| Mới xuất hiện | Angle mới có trong 3 tuần gần đây, trong khi sản phẩm đã chạy từ trước. Đây là tín hiệu store đang test angle mới |
| Ổn định / Đang giảm / Đã dừng | Angle đã dừng là angle đã thua |

Cột **tỉ trọng ad mới (4 tuần)** cho biết các store đang dồn creative vào angle nào.
Kết quả Claude được cache theo nội dung ad, nên tuần nào không có ad mới thì không gọi lại.
Báo cáo nằm ở `reports/angles.html` và `reports/angles.json`. Từ trang Ứng viên bấm "xem angle →" để mở phần angle của sản phẩm đó.

## Chạy

```bash
pip install -e ".[dev]"

winfinder demo                      # dữ liệu giả lập, không cần token → demo_out/winners.html, index.html, trends.html
pytest -q

export META_AD_LIBRARY_TOKEN=...    # token Ad Library API
winfinder run                       # collect → enrich → analyze → reports/index.html
winfinder analyze --enrich          # chấm lại từ DB hiện có
winfinder link                      # store → keyword sản phẩm → Trends → reports/winners.html
winfinder angles                    # angle/persona của từng sản phẩm → reports/angles.html
```

## Chạy tự động trên GitHub Actions

1. Tạo token: vào developers.facebook.com, tạo app, hoàn tất xác minh danh tính, rồi lấy access token có quyền truy cập Ad Library API.
2. Lưu token vào repo: **Settings → Secrets and variables → Actions → New secret**, đặt tên `META_AD_LIBRARY_TOKEN`.
3. (Tuỳ chọn) Thêm secret `SERPAPI_API_KEY` cho Trends. Không có thì workflow thử provider `direct`.
   (Nên có) Thêm secret `ANTHROPIC_API_KEY` để Claude đọc trang sản phẩm. Không có thì dùng heuristic, kém chính xác hơn.
4. Workflow **Weekly scan** chạy mỗi thứ Hai (hoặc bấm *Run workflow* để chạy tay). Lịch sử được lưu trong `data/winfinder.db`, báo cáo trong `reports/`, và cả hai được commit ngược lại vào repo.

Lưu ý: chạy hằng tuần mới tích lũy được lịch sử reach để đo tốc độ tăng thật.

## Cấu trúc

```
winfinder/
  sources/adlibrary.py   Meta Ad Library client (phân trang, xử lý rate limit)
  sources/domain.py      RDAP, crt.sh, Shopify products.json
  analysis/velocity.py   creative mới/tuần + nhận diện phase scale
  analysis/scoring.py    gom theo store, chấm điểm
  analysis/trends.py     tách mùa vụ, đo xu hướng, phân loại keyword/chủ đề
  sources/trends.py      SerpApi / Google Trends trực tiếp / CSV, keyword neo, Autocomplete
  trends_pipeline.py     chủ đề × thị trường, cache, ước TAM, mức độ lan rộng
  trends_report.py       báo cáo Trends
  analysis/keywords.py   rút keyword sản phẩm bằng heuristic (ad + trang sản phẩm)
  analysis/product_llm.py Claude hiểu sản phẩm: tên gọi chung, keyword tìm kiếm, keyword nhu cầu
  sources/product_page.py đọc trang sản phẩm (Shopify, JSON-LD, meta, headings), chọn hero product
  link.py                nối store ↔ Trends, kết luận, điểm tổng
  link_report.py         trang Ứng viên
  analysis/angles.py     Claude chia angle/persona, đo tăng trưởng từng angle
  angles_pipeline.py     chạy angle cho các sản phẩm ứng viên (cache theo nội dung ad)
  angles_report.py       trang Angle
  pipeline.py            collect / enrich
  report.py              xuất JSON + HTML
  demo.py                dữ liệu giả lập
  niches.py              danh sách ngách (niches.yaml)
  runner.py              một lần quét đầy đủ, dùng chung cho web và dòng lệnh
  server.py              giao diện web `winfinder serve`
  cli.py
```
