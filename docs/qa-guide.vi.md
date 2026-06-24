# Hướng dẫn QA — Hiểu nền tảng trước, rồi mới dùng

> Bản tiếng Việt của [qa-guide.md](qa-guide.md). Các **thuật ngữ kỹ thuật** (suite, test, run,
> group, case, step, action, assert, bridge, verdict, …) được **giữ nguyên tiếng Anh**.

**Đọc cái này trước.** Nó giải thích *từng thành phần là gì* và *cả hệ thống ghép lại ra sao*, bằng
ngôn ngữ đơn giản, trước khi bạn động vào bất kỳ file nào. Khi đã nắm được khái niệm, các câu lệnh
hằng ngày sẽ rất dễ.

- Mới vào và muốn hiểu ý tưởng? → đọc từ trên xuống.
- Chỉ cần câu lệnh và tên field chính xác? → xem playbook chi tiết
  [qa-workflow.md](qa-workflow.md) và file tự sinh `framework/schema_reference.md`.

---

## Mục lục

1. [Ý tưởng cốt lõi (trong một phút)](#1-ý-tưởng-cốt-lõi-trong-một-phút)
2. [Khái niệm cốt lõi — bộ từ vựng](#2-khái-niệm-cốt-lõi--bộ-từ-vựng)
3. [Trái tim của hệ thống: hai trọng tài (bridge + AI)](#3-trái-tim-của-hệ-thống-hai-trọng-tài-bridge--ai)
4. [File test, dựng dần từng phần](#4-file-test-dựng-dần-từng-phần)
5. [Group vs Test vs Run — khi nào dùng cái nào](#5-group-vs-test-vs-run--khi-nào-dùng-cái-nào)
6. [Ép kết quả (vì sao test không ngẫu nhiên)](#6-ép-kết-quả-vì-sao-test-không-ngẫu-nhiên)
7. [Toàn bộ quy trình, theo từng giai đoạn](#7-toàn-bộ-quy-trình-theo-từng-giai-đoạn)
8. [Đọc kết quả](#8-đọc-kết-quả)
9. [Bảng thuật ngữ & tra cứu nhanh](#9-bảng-thuật-ngữ--tra-cứu-nhanh)

---

## 1. Ý tưởng cốt lõi (trong một phút)

Nền tảng này là một QA tester tự động (robot) cho game slot. Bạn cung cấp cho nó:

- **Tài liệu của game** (game hoạt động thế nào), và
- **Ý tưởng test của bạn** (bạn muốn kiểm tra cái gì).

Sau đó nó **mở game thật trên trình duyệt, chơi game, quay video, và kiểm tra kết quả** — và nó
kiểm tra **theo hai cách cùng lúc**: hỏi trực tiếp code của game để lấy số liệu, *và* nhìn vào màn
hình bằng một AI đánh giá những gì người chơi nhìn thấy. Khi cả hai đồng ý, bạn tin được kết quả.
Khi chúng bất đồng, hệ thống gắn cờ để con người xem lại.

Ba trợ lý (chúng ta gọi là **skill**) làm phần nặng nhọc, và **mỗi skill đều dừng lại chờ bạn
duyệt** trước khi ghi bất cứ thứ gì:

| Skill | Bạn nói | Bạn nhận lại |
|-------|---------|--------------|
| `/init-knowledge` | "Đây là tài liệu game." | Một **knowledge file** sạch, có cấu trúc về game + một **danh sách asset**. |
| `/write-test` | "Test các kịch bản này." | Một **test suite** sẵn sàng chạy (file YAML). |
| `/run-test` | "Chạy suite này." | **Verdict** (pass/fail), một **video replay**, và một **chẩn đoán** cho mọi lỗi. |

Đó là toàn bộ nền tảng: **hiểu game → viết test → chạy test → đọc kết quả.**

---

## 2. Khái niệm cốt lõi — bộ từ vựng

Đây là những từ bạn sẽ gặp ở khắp nơi. Nắm được chúng thì phần còn lại chỉ là chi tiết.

### Các khối xây dựng của một file test

| Thuật ngữ | Ý nghĩa đơn giản | Liên tưởng |
|-----------|------------------|------------|
| **Suite** | Một file chứa toàn bộ test cho một game. | Một tài liệu *kế hoạch* test. |
| **Test** | Một kịch bản bạn muốn kiểm tra. Cho ra **một verdict**. | Một dòng việc: "bet tăng khi bấm +". |
| **Step** | Một việc bên trong test — hoặc *làm gì đó* hoặc *kiểm tra gì đó*. | Một bước trong công thức nấu ăn. |
| **Action** | Một step **làm** một việc (spin, click, navigate, wait). | "Khuấy nồi." |
| **Assert** (một "check") | Một step **kiểm chứng** điều gì đó là đúng. | "Nếm thử — đủ mặn chưa?" |
| **Setup** | Các step chạy **trước** để chuẩn bị. | "Làm nóng lò." |
| **Teardown** | Các step chạy **sau** để dọn dẹp. | "Rửa chén." |

### Các khái niệm về gom nhóm

| Thuật ngữ | Ý nghĩa đơn giản |
|-----------|------------------|
| **Group** | Một cụm test dùng chung một **bước chuẩn bị tốn kém** (ví dụ cùng một tỉ lệ đã ép). Phần setup/teardown của group chạy **một lần cho cả cụm**, không phải mỗi test một lần. |
| **Run** | **Một lượt chơi trả lời nhiều câu hỏi cùng lúc.** Thay vì chơi một vòng bonus năm lần để kiểm tra năm thứ, bạn chơi **một lần** và đọc ra năm verdict. |
| **Case** | Một câu hỏi (một verdict) **bên trong một run**. Một run có nhiều case. |

> **Test vs Run trong một dòng:** một **Test** = một lượt chơi → một verdict. Một **Run** = một
> lượt chơi → nhiều verdict (mỗi case một verdict). Chi tiết ở
> [phần 5](#5-group-vs-test-vs-run--khi-nào-dùng-cái-nào).

### Các khái niệm "làm sao biết là đúng?"

| Thuật ngữ | Ý nghĩa đơn giản |
|-----------|------------------|
| **The bridge** | **Điều khiển từ xa cho test** được nhúng sẵn trong game (`window.__gameTestBridge`). Nó cho phép test bấm nút và **hỏi code của game để lấy số liệu** ("còn bao nhiêu free spin?"). |
| **Bridge assert** | Một check hỏi bridge để lấy một số liệu chính xác. **Deterministic** — cùng input luôn cho cùng kết quả. |
| **AI assert** | Một check trong đó AI **nhìn màn hình (image) hoặc đoạn clip đã quay (video)** và đánh giá những gì người chơi thấy. Hợp cho phần hình ảnh mà con số thô không chứng minh hết được. |
| **Reference assets** | Ảnh chụp có nhãn của từng symbol/nút mà bạn đưa cho AI, để nó biết "Coin" hay "nút Spin" *trông như thế nào*. |
| **Verdict** | Kết quả cuối của một test/case sau khi kết hợp cả hai check: 🟢 green / 🟠 orange / 🔴 red / ⏸ pending / skip. |

### Các khái niệm "làm cho lặp lại được"

| Thuật ngữ | Ý nghĩa đơn giản |
|-----------|------------------|
| **Ép kết quả (forcing)** | Slot thật thì ngẫu nhiên. Để test "điều gì xảy ra khi trigger 3 coin", bạn phải **ép (force)** kết quả đó để test lặp lại được. (Hai cách — xem [phần 6](#6-ép-kết-quả-vì-sao-test-không-ngẫu-nhiên).) |
| **Variable (`save` / `{{name}}`)** | Bắt lấy một giá trị trong lúc test (ví dụ balance) và **dùng lại sau** trong một check. |

---

## 3. Trái tim của hệ thống: hai trọng tài (bridge + AI)

Đây là ý tưởng quan trọng nhất của nền tảng, nên nó có riêng một phần.

Mọi test có ý nghĩa đều được phán xử bởi **hai trọng tài độc lập**:

- **Trọng tài 1 — the bridge (bộ não của game).** Hỏi thẳng code. Chính xác và tức thì.
  *"`getFreeSpinsRemaining` → 3."* Không thể bị qua mặt, nhưng nó chỉ biết con số — không nói cho
  bạn biết các coin *trông có* đang bị khóa trên màn hình hay không.
- **Trọng tài 2 — the AI (đôi mắt của người chơi).** Xem màn hình hoặc đoạn clip và đánh giá như
  một con người. *"Đúng, ba coin đang sáng và đứng yên suốt các vòng re-spin."* Tuyệt cho phần hình
  ảnh, nhưng phán đoán thì không chính xác tuyệt đối.

Nền tảng gộp hai ý kiến của chúng thành một **verdict**:

| Verdict | Chuyện gì xảy ra | Làm gì |
|---------|------------------|--------|
| 🟢 **green** | Cả hai đồng ý là pass (hoặc không có AI check). | Tin được. ✅ |
| 🟠 **orange** | **Chúng bất đồng** — một bên pass, một bên fail. | **Con người xem lại replay.** Đây là tín hiệu giá trị. 👀 |
| 🔴 **red** | Cả hai cùng fail (hoặc test bị lỗi/timeout). | Một lỗi thật — cần điều tra. ❌ |
| ⏸ **pending** | AI video chưa chạy xong. | Chạy lại để có kết quả. |
| **skip** | Test không có check nào. | Không có gì để phán xử. |

> **Vì sao cần hai trọng tài?** Bridge chứng minh *phần toán* đúng; AI chứng minh *người chơi thấy*
> đúng thứ. Một bug có thể ẩn ở khoảng giữa hai bên — ví dụ code báo "win = 10" nhưng màn hình chẳng
> hiện gì. **Orange chính là hệ thống bảo bạn đi xem lại.**

---

## 4. File test, dựng dần từng phần

Một suite là một file `.yml` (YAML). Đừng lo phải học thuộc — `/write-test` và định dạng dễ đọc
`.flow.yml` (xem [authoring-flow.md](authoring-flow.md)) sinh ra nó cho bạn. Nhưng bạn nên *đọc
hiểu* được một file. Ta sẽ dựng nó lên từ con số không.

### 4.1 Bộ khung (mọi suite đều có)

```yaml
suite: "My first suite"     # tiêu đề cho người đọc
game: zeus                  # game nào
version: "1.0"
config:
  test_timeout_ms: 120000   # bỏ cuộc một test sau 2 phút
  suite_timeout_ms: 1200000 # bỏ cuộc cả suite sau 20 phút
  video: true               # quay video (cần cho các AI check dạng video)

tests:
  - ...                     # các test của bạn nằm ở đây
```

### 4.2 Một test = một id, một title, và một danh sách step

```yaml
tests:
  - id: ZZ-03                       # id duy nhất (dùng để chạy riêng test này)
    title: "Increase/Decrease Bet"  # nó kiểm chứng điều gì
    steps:
      - id: step-01
        action: bridge_action       # LÀM: bấm nút "tăng bet"
        params: { name: nextBet }
      - id: step-02
        action: assert              # KIỂM TRA: bet index đã thành 1 chưa?
        assert:
          bridge: { method: getBetIndex, expected: 1 }
```

Đọc là: **"Bấm tăng-bet, rồi kiểm tra bet index giờ bằng 1."** Mỗi step có một `id`. Một step hoặc
**làm** gì đó (`action:`) hoặc **kiểm tra** gì đó (`assert:`).

### 4.3 Thêm cả hai trọng tài vào cùng một check

Một step `assert` có thể mang **cả** một bridge check lẫn một AI check — đó chính là cơ chế hai
trọng tài trong thực tế:

```yaml
      - id: step-05
        assert:
          bridge:                              # Trọng tài 1: phần số liệu
            method: getFreeSpinsRemaining
            expected: 3
          ai:                                  # Trọng tài 2: phần hình ảnh
            mode: image
            ref_screenshot: step-04            # nhìn vào ảnh chụp nào
            assets: [ ui_elements.free_spin_counter ]   # chỉ cho AI biết cần tìm gì
            prompt: "Does the free-spin counter show 3?"
            output_schema: { shows_three: { type: boolean } }
            expected: { shows_three: true }
```

- **`mode: image`** phán xử một ảnh chụp đơn (do một step `capture` trước đó chụp).
- **`mode: video`** phán xử một *clip* của đoạn quay giữa hai step (`video_trim: { from_step,
  to_step }`) — dùng cho những thứ diễn ra theo thời gian, như "các coin bị khóa suốt các vòng
  re-spin."
- **`assets:`** (hoặc `asset_groups:`) cho AI biết nhìn vào reference image nào, để nó không phải
  đoán mò "Coin" trông ra sao.

### 4.4 Nhớ một giá trị rồi kiểm tra sau

Đôi khi giá trị cần kiểm tra chỉ biết được lúc chạy (ví dụ tiền thắng của một spin). **Save** nó
lại, rồi assert dựa trên nó sau. Một `"$name"` trong `args` của một check sẽ tham chiếu tới giá trị
đã lưu:

```yaml
      - id: step-05
        action: bridge_action
        params: { name: getLastPayout }
        save: payout                       # nhớ con số này với tên "payout"
      - id: step-06
        action: bridge_assert
        params:
          method: inRange                  # bridge.inRange(<payout>, 0, 100)
          args: [ "$payout", 0, 100 ]      # "$payout" = giá trị đã lưu
          expected: true
```

Bạn cũng có thể nhét một giá trị đã lưu vào prompt của **AI** bằng `{{name}}` — ví dụ
`prompt: "Is the bet shown as {{bet}}?"` — để AI xác nhận một giá trị *đã biết* thay vì đánh giá
mơ hồ.

---

## 5. Group vs Test vs Run — khi nào dùng cái nào

Ba thứ này là cấu trúc của một suite, và nhầm lẫn chúng là chỗ rối nhất. Đây là mô hình tư duy.

### Test vs Run nhìn nhanh

Một **test** và một **run** chạy *y hệt nhau* (setup → steps → teardown, cùng action và assert).
Khác biệt duy nhất là **cho ra bao nhiêu verdict**:

| | **Test** | **Run** |
|---|---|---|
| Số lượt chơi | một | một |
| Số verdict tạo ra | **1** | **nhiều** (mỗi `case` một verdict) |
| Báo cáo dưới dạng | một dòng | một dòng **mỗi case** |

Vậy run *không phải* là một loại check khác — nó là cách **thu nhiều verdict từ một lượt chơi**.
Dùng nó khi nhiều check cùng dựa trên *cùng một lượt chạy tốn kém* (ví dụ trigger một bonus một lần,
rồi phán xử sáu thứ về nó) thay vì chơi lại lượt đó cho từng check.

### Test — một lượt chơi, một verdict

Mặc định. Dùng cho một kịch bản độc lập.

```yaml
tests:
  - id: ZZ-03
    title: "Bet increases"
    steps: [ ... ]
```

### Group — các test dùng chung một setup tốn kém

Một số test cần cùng một bước chuẩn bị tốn kém trước (thường gặp nhất: **ép tỉ lệ (odds) của game**
về một kết quả cụ thể). Đặt chúng vào một **group** nghĩa là setup đó chạy **một lần** cho tất cả,
không phải mỗi test một lần — nhanh hơn nhiều.

```yaml
groups:
  - id: freespin-tests
    setup:    [ ... ép tỉ lệ về "free-spin trigger" ... ]   # chạy MỘT LẦN
    teardown: [ ... khôi phục tỉ lệ ... ]                    # chạy MỘT LẦN
    tests:
      - id: ZZ-30
        title: "Free spins start"
        steps: [ ... ]
      - id: ZZ-31
        title: "Counter counts down"
        steps: [ ... ]
```

> Chạy một test đơn bằng `--test ZZ-30` **vẫn chạy setup/teardown của group nó** — nên tỉ lệ vẫn
> đúng kể cả khi bạn chỉ chạy một test.

### Run — một lượt chơi, NHIỀU verdict (các case)

Đây là cái khéo léo. Một số kịch bản **tốn kém để tới được** (ví dụ phải trigger và chơi hết một
vòng bonus, mất hơn một phút). Bạn không muốn làm việc đó năm lần để trả lời năm câu hỏi. Một
**run** chơi nó **một lần** và đọc ra một verdict cho mỗi **case**:

```yaml
runs:
  - id: fs-3coin-run
    title: "Play one bonus, check several things about it"
    cases:                                   # mỗi case = một dòng verdict trong báo cáo
      - { id: ZZ-30, title: "Free spins triggered" }
      - { id: ZZ-32, title: "Coins stayed locked" }
      - { id: ZZ-35, title: "Returned to base game" }
    steps:
      - id: step-01
        action: bridge_action                # step thường (không gắn case): chỉ chơi bonus
        params: { name: spinUntilBonus }
      - id: step-02
        case: ZZ-30                           # check này kiểm chứng CASE ZZ-30
        assert: { bridge: { method: getFreeSpinsRemaining, expected: 3 } }
      - id: step-10                           # MỘT lần gọi AI video → verdict cho nhiều case
        assert:
          ai:
            mode: video
            video_trim: { from_step: step-01, to_step: step-09 }
            output_schema: { coins_held: {type: boolean}, returned_to_base: {type: boolean} }
            expected_by_case:                 # chia câu trả lời của AI theo từng case
              ZZ-32: { coins_held: true }
              ZZ-35: { returned_to_base: true }
```

Quy tắc then chốt cho run:

- **Gắn một check bằng `case:`** (một id, hoặc một danh sách) để nói nó kiểm chứng case nào.
- Một **step thường (không gắn case) mà fail là chí mạng** — nghĩa là lượt chơi đã hỏng, nên *mọi*
  case đều thành red (ta không thu được bằng chứng).
- Với một AI check, **`expected_by_case`** chia câu trả lời của một lần gọi model cho nhiều case —
  một lần gọi, nhiều verdict.
- Mỗi case hiện ra thành **một dòng riêng** trong báo cáo, tất cả dùng chung một video của run.
- Mỗi case vẫn được **hai trọng tài** (bridge + AI) phán xử và có verdict 🟢/🟠/🔴 riêng — phán xử
  y như một test, chỉ là quy về từng case.

| Bạn có… | Dùng… |
|---------|-------|
| Một kịch bản độc lập | **Test** |
| Nhiều test cần cùng một bước chuẩn bị tốn kém | **Group** (gồm các test) |
| Nhiều check về *cùng một lượt chơi tốn kém* | **Run** (với các case) |

---

## 6. Ép kết quả (vì sao test không ngẫu nhiên)

Slot thật thì ngẫu nhiên — nên "test xem điều gì xảy ra ở một spin thắng" là bất khả thi trừ khi
bạn **ép (force)** kết quả. Nền tảng ép kết quả theo hai cách; thường bạn không phải tự chọn
(`/write-test` chọn giúp), nhưng bạn nên hiểu sự khác nhau:

| Cách | Hoạt động ra sao | Hợp nhất cho |
|------|------------------|--------------|
| **Bridge injection** | Bảo game client "làm cho spin *tiếp theo* ra đúng cái này." Nhanh, phía client. | Một spin bị ép đơn lẻ. |
| **Back-office (BO) paytable** | Đổi tỉ lệ phía **server** để các spin chắc chắn hành xử theo một cách, rồi khôi phục sau. Đi đúng đường server thật. | Chuỗi nhiều spin (bonus, Hold & Spin) nơi server quyết định từng spin. |

Vì cách BO tốn kém (nó đổi trạng thái server), các test cần nó được gom vào một **group** để cú đổi
chỉ xảy ra một lần. Đó là lý do chính group tồn tại.

Bản thân dữ liệu deterministic nằm trong các **paytable fixture**
(`games/{game}/paytables/{name}.json`), do `/write-test` sinh ra từ các quy tắc paytable đã ghi
trong tài liệu game.

---

## 7. Toàn bộ quy trình, theo từng giai đoạn

```
MỘT LẦN cho mỗi game                        MỖI suite
────────────────────                        ────────────────
1. Bỏ tài liệu game vào docs/raw/   ┐       4. Viết test:
2. /init-knowledge  → knowledge     │            /write-test  HOẶC  một .flow.yml + compile
3. Thêm ảnh asset, bridge-api.md,   │       5. /run-test → verdict + replay + chẩn đoán
   và cấu hình env                  ┘       (lặp lại 4–5 cho mỗi suite)
```

### Giai đoạn 1 — Gom tài liệu game (`docs/raw/`)
Bỏ mọi thứ team game đưa cho bạn vào `games/{game}/docs/raw/`: **tài liệu thiết kế game**,
**base paytable** (`paytable.json`), và **quy tắc paytable** (`paytable.md`). Chất lượng của mọi
thứ phía sau phụ thuộc vào kho tài liệu này — các skill trích dẫn nó và từ chối bịa dữ liệu.

### Giai đoạn 2 — Hiểu game (`/init-knowledge`)
```bash
/init-knowledge --game=zeus
```
Đọc `docs/raw/`, phát hiện mọi cơ chế, và **đưa cho bạn một báo cáo để xác nhận**. Chỉ sau khi bạn
đồng ý, nó mới ghi:

| Artifact | Nó là gì |
|----------|----------|
| `docs/final/game-knowledge.md` | Nguồn sự thật sạch, có cấu trúc về game. |
| `docs/final/paytable.md` | Payout của symbol & quy tắc paytable (nếu có trong tài liệu). |
| `docs/assets/assets.yml` | **Asset manifest** — mọi symbol/UI element, gom nhóm (`symbols:`, `ui_elements:`, `ui:`), mỗi cái có một đường dẫn `file:`. |

> Onboard lại sau khi tài liệu đổi? Dùng `/update-knowledge` (nó phát hiện cái gì đã lệch).

### Giai đoạn 3 — Thêm ảnh reference & các API (làm tay)
- **Ảnh asset:** bỏ các ảnh cắt của symbol/UI vào `docs/assets/symbols/` và `docs/assets/ui/` sao
  cho đường dẫn khớp với các entry `file:` trong `assets.yml`. Chúng dạy AI biết mỗi symbol trông
  ra sao. *Chỉ ảnh tồn tại trên đĩa mới được dùng* — ảnh thiếu sẽ bị bỏ qua âm thầm (AI check yếu
  hơn).
- **Bridge API:** đặt `bridge-api.md` của team client tại `games/{game}/bridge-api.md`.
  `/write-test` kiểm tra mọi bridge method theo file này và từ chối bịa method.
- **Cấu hình môi trường:** secret ở `.env` (+ `.env.{env}`), và cấu hình game theo từng môi trường
  (launch URL, đăng nhập playsite, các id back-office) ở `games/{game}/env.config.{env}.yml`. Cách
  nhanh nhất: dán một launch URL còn hiệu lực vào `url:` và đặt key cho AI provider của bạn.

### Giai đoạn 4 — Viết test
Hoặc mô tả ý định và để `/write-test` sinh suite, hoặc tự viết một `{suite}.flow.yml` dễ đọc rồi
compile (`python -m framework.authoring.compile --game {game} --suite {suite}`; xem
[authoring-flow.md](authoring-flow.md)). Kết quả:
`games/{game}/test-suites/{suite}.yml` (+ các paytable fixture).

### Giai đoạn 5 — Chạy test (`/run-test`)
Lệnh này mở một headless browser, quay video, chơi từng test, chạy cả hai trọng tài, và ghi mọi thứ
vào `test-results/{game}/{env}/{suite}-{timestamp}/`.

Mọi lần chạy đều cần `--game`, `--suite`, và `--env`. Thứ bạn thêm vào quyết định **chạy bao nhiêu**:

| Để chạy… | Thêm… | Chuyện gì xảy ra |
|----------|-------|------------------|
| **Cả suite** | *(không gì)* | Mọi test, group, và run đều chạy. |
| **Một group** | `--group <group-id>` | Tất cả test + run trong group đó, với setup/teardown của group chạy một lần. |
| **Nhiều group** | `--group g1,g2` | Danh sách id group, ngăn bằng dấu phẩy. |
| **Một test** | `--test <test-id>` | Chỉ test đó — **nhưng setup/teardown của group nó vẫn chạy bao quanh**, nên tỉ lệ đã ép vẫn đúng. |
| **Nhiều test** | `--test ZZ-03,ZZ-05` | Danh sách id test, ngăn bằng dấu phẩy. |
| **Cả một run** | `--test <run-id>` | Run chạy một lần; **tất cả** case của nó được báo cáo. |
| **Một case của run** | `--test <case-id>` | Cả run vẫn chạy (các case dùng chung một lượt chơi), nhưng **chỉ verdict của case đó** được báo cáo. |
| **Các test trong một group** | `--group <g> --test <id>` | Các bộ lọc kết hợp — thu hẹp về group, rồi về (các) test bên trong. |

```bash
# cả suite
/run-test --game=zeus --suite=freespin --env=dev

# theo group
/run-test --game=zeus --suite=freespin --env=dev --group=fs-trigger3-countdown
/run-test --game=zeus --suite=freespin --env=dev --group=g1,g2          # nhiều group

# theo test
/run-test --game=zeus --suite=freespin --env=dev --test=ZZ-03
/run-test --game=zeus --suite=freespin --env=dev --test=ZZ-03,ZZ-05     # nhiều test

# theo run / theo case (một case id chạy cả run, báo cáo case đó)
/run-test --game=zeus --suite=freespin --env=dev --test=fs-3coin-run    # cả run, mọi case
/run-test --game=zeus --suite=freespin --env=dev --test=ZZ-32           # một case của run đó
```

> `/run-test` là skill (thêm phần trình bày kết quả). Runner trần nhận **cùng các flag**:
> `python framework/runner/suite_runner.py --game zeus --suite freespin --env dev [--group … | --test …]`.
> Các id của test, run, và case đều **duy nhất trong cả suite**, nên `--test <id>` không bao giờ
> nhập nhằng.

---

## 8. Đọc kết quả

`/run-test` in ra một bảng điều khiển (mỗi test/case một dòng, số đếm, chi phí AI). Với mỗi test bạn
có một thư mục artifact gồm:

| File | Nó cho thấy gì |
|------|----------------|
| `result.json` | Verdict và kết quả của cả hai trọng tài. |
| `step-N-ai.json` | Câu trả lời chính xác của AI cho một AI check. |
| `step-N.jpg` | Một ảnh chụp tại step đó. |
| `events.jsonl` / `timeline.json` | Luồng event thô và thời gian từng step. |
| **`replay.html`** | Một **replay tương tác** — video đồng bộ với log event. **Bắt đầu từ đây.** |

Khi một test là 🟠 **orange** hoặc 🔴 **red**, `/run-test` đưa ra một **chẩn đoán dựa trên bằng
chứng**: nó đọc các artifact đó cùng `game-knowledge.md` và cho bạn biết nguyên nhân là kỳ vọng sai,
một step dễ vỡ, một AI prompt định phạm vi sai, vấn đề hạ tầng (token/bridge), hay một **bug game**
có thể có.

> `/run-test` là **read-only** — nó không bao giờ sửa suite của bạn. Để đổi một test, quay lại
> `.flow.yml` của bạn (hoặc `/write-test`). Một **orange là tín hiệu để mở `replay.html`**: hai
> trọng tài thấy khác nhau, và mắt bạn là người phân xử.

---

## 9. Bảng thuật ngữ & tra cứu nhanh

### Thuật ngữ một dòng

- **Suite** — file chứa các test cho một game.
- **Test** — một kịch bản → một verdict.
- **Run** — một lượt chơi → nhiều verdict (các case).
- **Case** — một verdict bên trong một run.
- **Group** — các test dùng chung một setup tốn kém (chạy một lần).
- **Step** — một dòng trong test: một **action** (làm) hoặc một **assert** (kiểm tra).
- **Bridge** — điều khiển từ xa cho test của game; bridge check thì chính xác/deterministic.
- **AI assert** — một check màn hình/video do AI phán xử (`image` hoặc `video`).
- **Reference assets** — ảnh có nhãn dạy AI từng symbol/nút.
- **Forcing** — làm cho một game ngẫu nhiên cho ra một kết quả cố định để test lặp lại được.
- **Verdict** — 🟢 green / 🟠 orange (bất đồng → xem lại) / 🔴 red / ⏸ pending / skip.

### Mọi thứ nằm ở đâu

```
games/{game}/
├── docs/raw/                  # 1. tài liệu nguồn bạn bỏ vào
├── docs/final/                # 2. output của /init-knowledge: game-knowledge.md, paytable.md
├── docs/assets/
│   ├── assets.yml             #    asset manifest (nhóm: symbols / ui_elements / ui)
│   ├── symbols/*.jpg          # 3. ảnh reference của symbol
│   └── ui/*.jpg               #    ảnh reference của UI
├── bridge-api.md              # 4. các bridge method (từ team client)
├── env.config.{env}.yml       # 5. cấu hình theo môi trường (launch url, playsite, back-office)
├── paytables/{name}.json      #    fixture ép kết quả (từ /write-test)
└── test-suites/
    ├── vocab.yml              #    các verb riêng cho game, dùng cho authoring .flow.yml
    ├── {suite}.flow.yml       #    nguồn verb dễ đọc (tùy chọn)
    └── {suite}.yml            #    suite chạy được
test-results/{game}/{env}/{suite}-{timestamp}/   # output của lần chạy + replay.html
```

### Tra cứu lệnh nhanh

```bash
# 1. Hiểu game (sau khi bỏ tài liệu vào docs/raw/)
/init-knowledge --game=zeus
/update-knowledge --game=zeus            # sau khi tài liệu đổi

# 2. Viết một suite — chọn một cách
/write-test --game=zeus --suite=freespin --raw="<mô tả test của bạn>"
python -m framework.authoring.compile --game zeus --suite sample   # compile một .flow.yml

# 3. Chạy nó
/run-test --game=zeus --suite=freespin --env=dev
/run-test --game=zeus --suite=freespin --env=dev --test=ZZ-30      # một test/case
/run-test --game=zeus --suite=freespin --env=dev --group=freespin-tests
```

---

*Bước tiếp theo: playbook chi tiết [qa-workflow.md](qa-workflow.md) (mọi tùy chọn, cách cài đặt
chính xác), [authoring-flow.md](authoring-flow.md) (toàn bộ danh mục verb `.flow.yml`),
`framework/schema_reference.md` (mọi action & field), và
[bo-integration.md](bo-integration.md) (API paytable back-office).*
