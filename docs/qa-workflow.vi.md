# Quy trình QA — Từ tài liệu game đến bộ test chạy được

Tài liệu này mô tả cách một QA engineer thực sự sử dụng nền tảng để đưa (onboard) một game vào
hệ thống và tạo ra một bộ test chạy được. Đây là **cẩm nang thực hành**; muốn hiểu chi tiết bên
trong của runner xem [framework-runner-guide.md](framework-runner-guide.md), còn schema thì xem
file tự sinh `framework/schema_reference.md`.

Nền tảng có sự hỗ trợ của AI: bạn cung cấp tài liệu game, ảnh tham chiếu (reference images),
bridge API và ý tưởng test; còn ba **skill** của Claude sẽ làm phần nặng:

| Skill | Lệnh | Nhận vào … | … cho ra |
|-------|------|------------|----------|
| **init-knowledge** | `/init-knowledge --game=<g>` | tài liệu game thô bạn bỏ vào `docs/raw/` | `game-knowledge.md` + `paytable.md` + `symbols.yml` có cấu trúc |
| **write-test** | `/write-test --game=<g> --suite=<s>` | ý tưởng test của bạn (xlsx/csv/text/`--raw`) + kiến thức đã có | một bộ test YAML chạy được + các file paytable ép kết quả |
| **run-test** | `/run-test --game=<g> --suite=<s> --env=<e>` | bộ test YAML | một lần chạy với kết quả green/orange/red + replay + chẩn đoán lỗi |

> Các skill trình bày phát hiện và **chờ bạn xác nhận** ở từng cổng kiểm soát (phát hiện cơ chế
> game, kế hoạch test, từng test riêng lẻ). Chúng không bao giờ tự ý ghi kiến thức hay test sau
> lưng bạn — bạn luôn nắm quyền kiểm soát.

---

## Toàn cảnh quy trình từ đầu đến cuối

```
┌─ MỘT LẦN cho mỗi game ─────────────────────────────────────────────────────────────┐
│                                                                                    │
│  1. Bỏ tài liệu game vào   →  games/{game}/docs/raw/                                │
│       • tài liệu game (PDF/Markdown/Excel mô tả cơ chế)                             │
│       • paytable gốc        →  paytable.json                                        │
│       • quy tắc paytable     →  paytable.md  (giải thích tham số; tham chiếu .json) │
│                                                                                    │
│  2. /init-knowledge        →  games/{game}/docs/final/game-knowledge.md            │
│                               games/{game}/docs/final/paytable.md                  │
│                               games/{game}/docs/assets/symbols.yml                 │
│                                                                                    │
│  3. Thêm ảnh tham chiếu    →  games/{game}/docs/assets/symbols/*.jpg               │
│       (đường dẫn PHẢI khớp với mục file: trong symbols.yml)  docs/assets/ui/*.jpg  │
│                                                                                    │
│  4. Thêm bridge API        →  games/{game}/bridge-api.md  (từ team client)         │
│                                                                                    │
│  5. Cấu hình môi trường    →  .env  (+ .env.{suffix})   ở root, bí mật dùng chung   │
│                               games/{game}/env.config.{suffix}.yml  theo môi trường │
│                                                                                    │
└────────────────────────────────────────────────────────────────────────────────────┘
┌─ CHO MỖI BỘ TEST (lặp lại) ────────────────────────────────────────────────────────┐
│                                                                                    │
│  6. Soạn test case         →  xlsx / csv / file text / mô tả --raw                 │
│     /write-test            →  games/{game}/test-suites/{suite}.yml  (+ paytables)  │
│                                                                                    │
│  7. /run-test              →  test-results/{game}/{env}/{suite}-{DD-MM-YYYY-HHMM}/ │
│                               kết quả + replay.html + chẩn đoán nếu có lỗi          │
│                                                                                    │
└────────────────────────────────────────────────────────────────────────────────────┘
```

Bước 1–5 làm **một lần** khi onboard game (và làm lại khi game thay đổi). Bước 6–7 lặp lại cho
mỗi bộ test bạn viết.

---

## Chuẩn bị — cài đặt Python (một lần)

Cài các thư viện Python (dùng Python hệ thống của bạn — sau đó không cần activate virtualenv):

```bash
python -m pip install -r requirements.txt
```

Cài binary trình duyệt cho Playwright (một lần):

```bash
python -m playwright install chromium
```

Kiểm tra mọi import hoạt động:

```bash
python -c "import playwright, google.genai, google.generativeai, requests, dotenv, yaml, PIL, imagehash, openpyxl, pdfplumber, csv; from playwright.async_api import async_playwright; print('All imports OK')"
```

Vậy là xong — không cần bước activate cho các lần chạy sau. (`ffmpeg` cũng phải có trên PATH; đây
là dependency hệ thống, không phải gói pip.)

---

## Giai đoạn 1 — Thu thập tài liệu game (`docs/raw/`)

Mọi thứ nền tảng biết về một game đều bắt đầu từ tài liệu nguồn thô mà bạn đặt vào:

```
games/{game}/docs/raw/
```

Bỏ vào bất cứ thứ gì team game/client đưa cho bạn — nền tảng đọc được **PDF, Excel (.xlsx),
ảnh (.png/.jpg) và Markdown**. Tối thiểu bạn cần:

| File | Là gì | Ghi chú |
|------|-------|---------|
| **Tài liệu game** | Bản mô tả cơ chế (vd. `Doc Zappin zeus.md`) — symbol, lưới (grid), payline, luật bonus | Nguồn chính mà `init-knowledge` đọc để phát hiện cơ chế. |
| **Paytable gốc** | `paytable.json` — bảng paytable / bảng trọng số (weight) nền của game, đúng như server dùng | Dữ liệu payout + weight có thẩm quyền. |
| **Quy tắc paytable** | `paytable.md` — bản giải thích **các tham số** của paytable do người viết, và **bắt buộc tham chiếu** `paytable.json` | Giải thích mỗi trường/weight nghĩa là gì và *server lấy mẫu (sample) kết quả từ weight ra sao*. Chính điều này sau đó cho phép công cụ tinh chỉnh weight để ép một kết quả xác định. |

> **Vì sao `paytable.md` phải tham chiếu `paytable.json`:** file `.md` là *cuốn luật* (các tham số
> nghĩa là gì và kết quả được rút ra thế nào); file `.json` là *dữ liệu*. Công cụ ép một kết quả
> spin cụ thể (xem Giai đoạn 6) đọc luật từ `.md` và cấu trúc/weight từ `.json` cùng lúc. Một file
> `.md` chỉ giải thích tham số mà không trỏ tới paytable gốc cụ thể sẽ khiến trình sinh phải đoán.

Giai đoạn này không có gì để chạy — bạn chỉ đang gom nguồn tài liệu. Chất lượng của mọi thứ phía
sau phụ thuộc vào nó (các skill trích dẫn nguyên văn từ các file này và từ chối bịa ra dữ liệu
không có ở đây).

---

## Giai đoạn 2 — Sinh kiến thức game (`/init-knowledge`)

```
/init-knowledge --game=zeus
```

Lệnh này nạp `docs/raw/`, phát hiện **mọi** cơ chế được tài liệu hoá (cơ chế dùng chung như
hold-and-spin / free-spin / buy-bonus / auto-spin, cộng với các cơ chế riêng của game), và phân
loại mỗi cơ chế thành **CONFIRMED / LIKELY / ABSENT / UNKNOWN** kèm trích dẫn nguyên văn làm bằng
chứng.

**Skill sẽ dừng lại và đưa bạn một báo cáo phát hiện** — cơ chế, symbol, grid/payline, và mỗi cơ
chế so với mẫu dùng chung chuẩn của nó như thế nào (Matches / Deviates — Khớp / Lệch). **Bạn xác
nhận hoặc sửa.** Chỉ sau khi bạn phản hồi, skill mới ghi các file kiến thức:

| Kết quả ghi ra | Mục đích |
|----------------|----------|
| `games/{game}/docs/final/game-knowledge.md` | Nguồn chuẩn có cấu trúc: tổng quan, symbol, payline, cơ chế (kèm Matches/Deviates từng cơ chế), giới hạn, chỉ mục nguồn. |
| `games/{game}/docs/final/paytable.md` | Payout từng symbol, payout đặc biệt/bonus, quy tắc paytable — trích từ tài liệu thô (chỉ tạo nếu có paytable được tài liệu hoá). |
| `games/{game}/docs/assets/symbols.yml` | **Manifest tài sản** — mọi symbol/phần tử UI, mỗi cái có đường dẫn `file:` và một khoá `ref_asset` (dùng sau trong test YAML). |
| `games/{game}/.docs-hash.json` | Manifest SHA-256 của nguồn tài liệu, để `/update-knowledge` phát hiện khi tài liệu thô bị thay đổi. |

Hãy rà soát `game-knowledge.md` và `paytable.md` cho chính xác trước khi đi tiếp — chúng nuôi
trực tiếp việc sinh test, nên một giá trị sai ở đây sẽ thành một test sai về sau.

> Onboard lại sau khi tài liệu thay đổi: dùng `/update-knowledge` (nó so sánh với `.docs-hash.json`)
> chứ không chạy lại `/init-knowledge`.

---

## Giai đoạn 3 — Thêm tài sản tham chiếu (`docs/assets/`)

`init-knowledge` ghi ra **manifest** (`symbols.yml`), nhưng **ảnh** tham chiếu thực tế — các ảnh
cắt (crop) của symbol và UI lấy từ game client — là thứ **bạn** tự thêm. Chúng nằm ở:

```
games/{game}/docs/assets/symbols/     # ảnh cắt symbol:  Coin.jpg, Wild.jpg, Pentagon.jpg, …
games/{game}/docs/assets/ui/          # ảnh cắt UI:      spin-button.jpg, free-spin-counter.jpg, …
```

**Đường dẫn file phải khớp chính xác với mục `file:` trong `symbols.yml`.** Manifest đó là một dict
khoá theo tên `ref_asset`; `file:` của mỗi mục **tính tương đối từ `docs/assets/`**:

```yaml
# games/zeus/docs/assets/symbols.yml  (trích)
symbols:
  coin:                                    # <- khoá này là ref_asset dùng trong test YAML
    file: symbols/Coin.jpg                 # <- phải tồn tại tại docs/assets/symbols/Coin.jpg
    name: "Coin (Hold & Spin)"
    type: bonus
    description: "Bronze-gold coin … landing 3+ triggers Hold and Spin."
ui_elements:
  free_spin_counter:
    file: ui/free-spin-counter.jpg         # <- phải tồn tại tại docs/assets/ui/free-spin-counter.jpg
    name: "Free Spin Counter"
    type: ui
    description: "Remaining re-spin count shown during the bonus."
```

**Vì sao điều này quan trọng:** lúc chạy, framework dựng một **gói tham chiếu symbol** (symbol
reference pack) từ `symbols.yml` và chèn các ảnh đã gắn nhãn đó vào *trước mọi* assertion AI, để mô
hình có "chân lý nền" (ground-truth) về hình dạng từng symbol/phần tử UI. Quy tắc này nghiêm nhưng
âm thầm:

> **Chỉ những tài sản có `file:` thực sự tồn tại trên đĩa mới được nạp vào gói tham chiếu AI.**
> Một mục manifest trỏ tới ảnh không tồn tại sẽ **bị bỏ qua âm thầm** — không báo lỗi, chỉ làm
> assertion AI yếu đi. (Trong manifest Zeus hiện tại, `crow`/`cup`/`ring`/`time` được liệt kê nhưng
> mang ghi chú `# image not yet added` — chúng sẽ không đóng góp vào việc chấm điểm của AI cho tới
> khi file jpg được bỏ vào.)

Vậy nên: với bất kỳ symbol/phần tử UI nào mà một test sẽ kiểm chứng bằng hình ảnh, hãy bảo đảm ảnh
của nó có mặt và đường dẫn khớp với manifest. Cắt tài sản từ game client, lưu vào đúng thư mục dưới
`docs/assets/`, và giữ tên file y hệt `file:` trong manifest.

---

## Giai đoạn 4 — Thêm bridge API (`games/{game}/bridge-api.md`)

Test điều khiển và quan sát game thông qua **bridge** (`window.__gameTestBridge`) — các phương thức
như `spin()`, `getAppState()`, `getBalance()`, `getFreeSpinsRemaining()`, `startGame()`,
`spinUntilBonus()`, các analyser `verifyHoldSpin*`, v.v. Team client làm chủ bề mặt này và tài liệu
hoá nó. Đặt tài liệu của họ tại:

```
games/{game}/bridge-api.md
```

`write-test` kiểm tra **mọi** tên phương thức bridge trong một test với file này (và/hoặc bề mặt
bridge mô tả trong `game-knowledge.md`). Nếu một phương thức không có trong tài liệu, skill sẽ
**báo cho bạn kèm chữ ký (signature) gợi ý** thay vì tự bịa — đó là cách ngăn các phương thức "ma"
lọt vào bộ test.

> Giữ file này đồng bộ với bản build của client. Khi team client thêm hoặc đổi tên một phương thức
> bridge, hãy cập nhật `bridge-api.md` trước, rồi mới viết test dùng nó. Nếu không có tài liệu bridge
> ở cả `bridge-api.md` lẫn `game-knowledge.md`, `write-test` sẽ từ chối chạy.

---

## Giai đoạn 5 — Cấu hình môi trường

Cấu hình được tách làm hai nơi theo **phạm vi**: bí mật dùng chung trong `.env` ở root (+ lớp phủ
theo môi trường), và cấu hình game theo từng môi trường trong `games/{game}/env.config.{suffix}.yml`.
Tên môi trường bạn truyền vào `--env` chọn bộ cấu hình qua bảng ánh xạ hậu tố (suffix):

| `--env` | lớp phủ `.env` | file cấu hình game |
|---------|----------------|--------------------|
| `dev` | `.env.dev` | `env.config.dev.yml` |
| `staging` | `.env.stg` | `env.config.stg.yml` |
| `prod` | `.env.prod` | `env.config.prod.yml` |
| `local` | `.env.local` | `env.config.local.yml` |

### 5a. `.env` ở root (+ `.env.{suffix}`) — bí mật & nhà cung cấp AI

`.env` nền giữ các bí mật dùng chung cho mọi môi trường; `.env.{suffix}` phủ lên các giá trị riêng
của từng môi trường (nó ghi đè `.env` nền). Sao chép các template `.env.example` /
`.env.{suffix}.example` rồi điền:

| Biến | Ở đâu | Mục đích |
|------|-------|----------|
| `GEMINI_API_KEY` | `.env` nền | Khoá Gemini cho assertion AI ảnh/video (bắt buộc khi `AI_PROVIDER=gemini`). |
| `AI_PROVIDER` | `.env` nền | `gemini` (mặc định) · `claude` (sub-agent Claude CLI cục bộ) · `local` (endpoint tự host). |
| `GEMINI_IMAGE_MODEL` / `GEMINI_VIDEO_MODEL` | `.env` nền | Ghi đè model AI (mặc định: `gemini-2.5-flash` / `gemini-3-flash-preview`). |
| `GEMINI_VIDEO_RESOLUTION` | `.env` nền | `LOW` · `MEDIUM` (mặc định) · `HIGH` — chi phí token mỗi khung hình video. |
| `LOCAL_AI_ENDPOINT` | `.env` nền | Endpoint HTTP `/analyze`, dùng khi `AI_PROVIDER=local`. |
| `CLAUDE_AI_FPS` / `CLAUDE_MODEL` / `CLAUDE_CLI_TIMEOUT` | `.env` nền | Các tham số của provider Claude (tốc độ lấy mẫu khung hình video, model, timeout). |
| `PLAYSITE_API_URL` / `PLAYSITE_USER` / `PLAYSITE_PASS` | `.env.{suffix}` | Đăng nhập Playsite để giải (resolve) URL game động. |
| `BO_API_BASE` | `.env.{suffix}` | URL gốc API back-office để điều khiển paytable (không có ở prod). |
| `BO_TOKEN` / `BO_REFRESH_TOKEN` | `.env.{suffix}` | Token truy cập / làm mới của BO (lấy từ phiên DevTools của BO). |

### 5b. `games/{game}/env.config.{suffix}.yml` — cấu hình game theo môi trường

Mỗi môi trường một file tự chứa (không có file nền dùng chung, không có lớp bọc `environments:`).
Ví dụ cho `dev`:

```yaml
# games/zeus/env.config.dev.yml
url: "http://192.168.1.13:7456/.../index.html?data=eyJ..."   # nếu khác rỗng thì dùng trực tiếp
url_script: "python scripts/get_game_url_playsite.py --env=dev"  # dự phòng khi url: ""
browser_params: "?bridge=1&debug=1"   # nối vào URL (bridge=1 bật việc thu thập event)

playsite:                # dùng để lấy link chơi (play link)
  game_code: sl_33_zappin_zeus
  currency: TRY          # bỏ trống để dùng ví mặc định của người chơi

bo:                      # định danh back-office cho các bước bo_paytable_* (spin xác định)
  game_id: 63
  sas_entity_id: 1
  agent_id: 1
  currency_id: 18
  game_code: SL_33_ZAPPIN_ZEUS
```

**Các khoá launch ở cấp trên cùng:**

| Khoá | Ý nghĩa |
|------|---------|
| `url` | Một URL launch dùng được ngay. **Nếu khác rỗng thì được dùng nguyên trạng** — cách nhanh nhất để chạy thủ công: dán một URL mới vào đây. |
| `url_script` | Lệnh chạy để giải URL khi `url` rỗng (luồng đăng nhập Playsite). Stdout của nó trở thành URL. |
| `browser_params` | Chuỗi query nối vào URL. **`?bridge=1` chính là thứ bật bridge test + thu thập event**; `&debug=1` thêm log debug. |

**`playsite:` — dùng để lấy link chơi** (được tiêm vào dưới dạng `PLAYSITE_*` cho `url_script`):

| Khoá | Ý nghĩa |
|------|---------|
| `game_code` | Mã game của game cần test — xác định Playsite sẽ mở game nào. |
| `currency` | Loại tiền (currency) bạn muốn test cho game đó (vd. `TRY`). Bỏ trống để dùng ví mặc định của người chơi. |

**`bo:` — định danh back-office** cho các bước `bo_paytable_*` ép kết quả xác định từ server (chỉ
có ở nơi dùng spin xác định — dev/stg/local, không có ở prod):

| Khoá | Ý nghĩa |
|------|---------|
| `game_id` | ID của game đó trong back-office. |
| `sas_entity_id` | SAS entity — **cố định bằng `1`**. |
| `agent_id` | ID của agent mà bạn chạy game trên đó. |
| `currency_id` | Loại tiền mà bạn chạy game trên đó (currency ID trong BO). |
| `game_code` | Mã game của game đó trong back-office (vd. `SL_33_ZAPPIN_ZEUS`). |

> **Bắt đầu nhanh:** để chạy thủ công, cách dựng nhanh nhất là dán một URL launch còn hiệu lực vào
> `url:` trong `env.config.dev.yml` và đặt `GEMINI_API_KEY` của bạn vào `.env`. Cấu hình Playsite/BO
> chỉ cần khi cần giải URL động và dùng paytable xác định phía server.

---

## Giai đoạn 6 — Soạn test case & sinh bộ test (`/write-test`)

Bạn không viết YAML bằng tay. Bạn diễn đạt **ý định** (intent) của test, và `write-test` chuyển nó
thành một bộ test hợp lệ qua một vòng phê duyệt. Cung cấp ý định theo bất kỳ dạng nào tiện cho bạn:

| Dạng đầu vào | Cách làm |
|--------------|----------|
| **Bảng tính** (`.xlsx`) | Để các case trong một sheet (file `test-case.xlsx` của repo là mẫu). Nói cho skill biết file nào, hoặc dán các dòng vào. |
| **File CSV / text** | Một file mô tả mỗi-test-một-dòng / mỗi-đoạn. |
| **Text trực tiếp** | Truyền mô tả thẳng bằng `--raw="…"`. |
| **Chỉ từ kiến thức** | Bỏ `--raw`; skill sinh độ phủ từ `game-knowledge.md` + các mẫu cơ chế dùng chung, có thể thu hẹp bằng `--scope`. |

```
/write-test --game=zeus --suite=suite-02-freespin --scope="free spin"
/write-test --game=zeus --suite=suite-02-freespin --raw="Trigger free spins with 3 coins; verify counter shows 3 then counts to 0"
```

**Skill làm gì (và dừng lại chờ bạn ở đâu):**

1. **Nạp kiến thức** — `game-knowledge.md`, `paytable.md`, `symbols.yml`, `bridge-api.md`, và
   `framework/schema_reference.md` (tài liệu tham chiếu action/schema có thẩm quyền).
2. **Lập kế hoạch bộ test** — ghi ra một `{suite}.plan.json`, gom test theo paytable/kết quả ép mà
   chúng cần, và **đưa toàn bộ kế hoạch cho bạn duyệt** (tập test, cách gom nhóm, paytable).
3. **Vòng phê duyệt từng test** — trình bày **mỗi lần một test** (các bước, assertion bridge + AI,
   và lựa chọn paytable) và chờ `yes` / `change` / `skip` trước khi ghi. Cổng này là bắt buộc —
   không có gì được ghi cho tới khi từng test được xác nhận riêng.
4. **Ghi bộ test** → `games/{game}/test-suites/{suite}.yml`.
5. **Sinh các paytable ép kết quả** → với mỗi kịch bản cần một spin xác định phía server, một
   sub-agent đọc **paytable gốc** và **quy tắc paytable** rồi tinh chỉnh weight để ép kết quả mong
   muốn, ghi ra `games/{game}/paytables/{ref}.json`. Bất cứ thứ gì nó không suy ra được từ luật đã
   tài liệu hoá sẽ bị đánh dấu `# PAYTABLE UNRESOLVED` chứ không đoán bừa.
6. **Báo cáo độ phủ** — những gì đã ghi, đã bỏ qua, những cơ chế/phương thức bridge/tài sản nào
   được phủ, và mọi điểm mơ hồ đã được gắn cờ.

**Một test được sinh ra chứa gì (để bạn đọc được YAML):** mỗi test ghép một **assertion bridge**
(kiểm tra trạng thái xác định) với một **assertion thị giác AI** (cái người chơi thực sự thấy) —
vd. bridge xác nhận `getFreeSpinsRemaining == 3` trong khi một assertion video Gemini xác nhận ba
coin bị khoá trên lưới. Tính xác định đến từ hoặc **bridge injection** (nhanh, phía client) hoặc
**paytable back-office** (đường kết quả thật phía server); skill chọn theo từng test và gom các lần
chuyển paytable sao cho chúng xảy ra một lần cho mỗi nhóm, không phải một lần cho mỗi test.

> Nếu một test cần một phương thức bridge không có trong `bridge-api.md`, hoặc tham chiếu một khoá
> tài sản thiếu trong `symbols.yml`, skill sẽ gắn cờ trong vòng phê duyệt — hãy sửa nguồn
> (Giai đoạn 3 / Giai đoạn 4) thay vì cố ép test đi tiếp.

---

## Giai đoạn 7 — Chạy bộ test (`/run-test`)

```
/run-test --game=zeus --suite=suite-02-freespin --env=dev
```

Lệnh này tương đương chạy runner trực tiếp (skill chỉ thêm phần trình bày kết quả lên trên):

```bash
python framework/runner/suite_runner.py --game zeus --suite suite-02-freespin --env dev
# chạy một test (setup/teardown của nhóm vẫn chạy, nên paytable của nó vẫn đúng):
python framework/runner/suite_runner.py --game zeus --suite suite-02-freespin --env dev --test ZZ-30
# chạy nguyên một nhóm:
python framework/runner/suite_runner.py --game zeus --suite suite-02-freespin --env dev --group freespin
```

Runner mở một trình duyệt headless, quay video, chạy từng test, rồi xử lý các assertion AI và ghi
mọi thứ vào:

```
test-results/{game}/{env}/{suite}-{DD-MM-YYYY-HHMM}/
```

**Kết quả (verdict)** kết hợp hai bộ kiểm chứng (bridge + AI):

| Kết quả | Ý nghĩa |
|---------|---------|
| 🟢 **green** | bridge đạt **và** AI đạt (hoặc không có assertion AI) — đạt hoàn toàn |
| 🟠 **orange** | hai bên không nhất trí — một đạt, một trượt → **cần người xem lại** |
| 🔴 **red** | cả hai cùng trượt (hoặc timeout/lỗi) |
| ⏸ **pending** | AI video chưa xử lý xong (chạy lại để có kết quả) |
| **skip** | test không có assertion nào |

`run-test` in ra một bảng điều khiển (từng dòng test, số đếm, chi phí Gemini), và với mỗi lỗi nó
chỉ cho bạn các tài liệu chứng cứ (artifact): `result.json`, phản hồi AI `step-N-ai.json`, ảnh chụp
`step-N.jpg`, `events.jsonl` / `timeline.json`, và một **`replay.html`** tương tác (video đồng bộ
với luồng event). Khi red/orange, nó đề nghị một **cuộc điều tra dựa trên chứng cứ** — đọc các
artifact đó và `game-knowledge.md` để nói cho bạn biết nguyên nhân là kỳ vọng sai, một bước mong
manh (fragile), một prompt AI bị khoanh vùng sai, hạ tầng (token/bridge), hay có thể là một bug
game.

> `run-test` là **chỉ-đọc** — nó không bao giờ sửa bộ test của bạn. Muốn đổi một test, quay lại
> `/write-test` (vd. `--raw="<thay đổi>"`). Một kết quả 🟠 orange là tín hiệu để xem replay: bridge
> và AI đã thấy hai thứ khác nhau.

---

## Mọi thứ nằm ở đâu

```
qa-platform/
├── .env, .env.dev, .env.stg, …          # bí mật + provider AI (Giai đoạn 5a)
├── framework/                            # runner + helper (đừng sửa để viết test)
│   └── schema_reference.md / .yml        # tham chiếu action & schema có thẩm quyền
├── shared-mechanisms/                    # các mẫu chuẩn: hold-and-spin, free-spin, …
├── scripts/get_game_url_playsite.py      # bộ giải URL dùng bởi url_script
├── games/{game}/
│   ├── docs/raw/                         # 1. tài liệu nguồn: tài liệu game + paytable.json + paytable.md
│   ├── docs/final/                       # 2. đầu ra init-knowledge: game-knowledge.md, paytable.md
│   ├── docs/assets/
│   │   ├── symbols.yml                   #    manifest tài sản (khoá ref_asset + đường dẫn file:)
│   │   ├── symbols/*.jpg                 # 3. ảnh tham chiếu symbol (khớp symbols.yml)
│   │   └── ui/*.jpg                      #    ảnh tham chiếu UI
│   ├── bridge-api.md                     # 4. bề mặt phương thức bridge (từ team client)
│   ├── env.config.{suffix}.yml           # 5. cấu hình game theo môi trường (url, playsite, bo)
│   ├── paytables/                        # 6. file ép kết quả do write-test sinh ra
│   │   ├── base.json, paytable.md        #    dữ liệu gốc + luật dùng để suy ra các file
│   │   └── {ref}.json
│   └── test-suites/
│       ├── {suite}.plan.json             #    artifact kế hoạch của write-test
│       └── {suite}.yml                   # 6. bộ test chạy được
└── test-results/{game}/{env}/{suite}-{DD-MM-YYYY-HHMM}/   # 7. đầu ra lần chạy + replay.html
```

## Bảng tra cứu lệnh nhanh

```bash
# Onboard một game (sau khi đã bỏ tài liệu vào games/{game}/docs/raw/)
/init-knowledge --game=zeus

# (rồi thêm ảnh docs/assets, bridge-api.md, và cấu hình env bằng tay)

# Soạn một bộ test từ một ý tưởng
/write-test --game=zeus --suite=suite-02-freespin --scope="free spin"
/write-test --game=zeus --suite=suite-02-freespin --raw="<mô tả test của bạn>"

# Chạy nó
/run-test  --game=zeus --suite=suite-02-freespin --env=dev
/run-test  --game=zeus --suite=suite-02-freespin --env=dev --test=ZZ-30   # một test/case
/run-test  --game=zeus --suite=suite-02-freespin --env=dev --group=freespin

# Làm mới kiến thức sau khi tài liệu game thay đổi
/update-knowledge --game=zeus
```

---

*Tài liệu liên quan: [framework-runner-guide.md](framework-runner-guide.md) (bên trong runner),
`framework/schema_reference.md` (schema của bộ test), [bo-integration.md](bo-integration.md)
(API paytable back-office).*
