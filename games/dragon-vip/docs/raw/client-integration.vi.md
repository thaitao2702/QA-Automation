# Premium Reel — Hướng dẫn tích hợp cho Client (GDP-2770)

> Bản tiếng Việt của [`client-integration.md`](client-integration.md). Nếu có khác biệt,
> bản tiếng Anh là bản chuẩn.

Premium Reel cho phép người chơi trả thêm phí để **nâng cấp một hoặc nhiều cuộn** sang
bộ phân phối (distribution) có tỷ lệ symbol giá trị cao hơn, áp dụng cho **vòng hiện tại**.

- **Phạm vi engine:** chỉ DefaultSlots. Với engine khác, tính năng không tồn tại —
  `settings.premiumReel` sẽ là `null` và server từ chối mọi bet request có Premium Reel.
- **Tất cả JSON key client nhận được đều là camelCase** (theo chuẩn response).
- Premium Reel và **Ante Bet loại trừ lẫn nhau** — không bao giờ gửi cả hai trong một bet.
  Premium Reel cũng bị chặn trong Promo và Buy Bonus.

---

## 1. Ý nghĩa `reelId`

`reelId` (trong bet request/response gọi là `id`) là một **định danh gói (package) không
minh bạch (opaque)**. Không tự suy diễn `23` = "reel 2 + reel 3" trong logic client —
luôn tra cứu dựa trên danh sách option lấy từ `gameInfo`.

| `reelId` | Ý nghĩa |
| --- | --- |
| `2` | Nâng cấp reel 2 |
| `3` | Nâng cấp reel 3 |
| `23` | Combo: nâng cấp cả reel 2 **và** reel 3 trong cùng một spin |

Có những `reelId` nào và multiplier bao nhiêu là **do paytable quy định** — đọc từ
`gameInfo`, không hardcode.

---

## 2. Response `gameInfo` — các field mới

### 2.1 `result.settings.premiumReel`

Cấu hình tính năng. Chỉ có khi game hỗ trợ Premium Reel và tính năng đang được bật
trong paytable; nếu không thì `null`.

```jsonc
{
  "result": {
    "settings": {
      "minBet": 1,
      "maxBet": 500,
      // ... các field cũ ...
      "premiumReel": {
        "enable": true,
        "options": [
          { "reelId": 2,  "multiplier": 1.5 },
          { "reelId": 3,  "multiplier": 1.5 },
          { "reelId": 23, "multiplier": 2.2 }
        ]
      }
    }
  }
}
```

| Field | Kiểu | Ý nghĩa |
| --- | --- | --- |
| `settings.premiumReel` | object \| `null` | `null` → tính năng không khả dụng / bị tắt → **ẩn toàn bộ UI Premium Reel**. |
| `settings.premiumReel.enable` | bool | Luôn là `true` khi object tồn tại. (Tính năng bị tắt hẳn trả về `null`, **không** phải `{ "enable": false }`.) |
| `settings.premiumReel.options` | array | Danh sách gói có thể chọn. Render các nút mua từ danh sách này. |
| `options[].reelId` | int | Id gói — gửi lại giá trị này ở `premiumReel.id` trong bet request. **Ở đây key tên là `reelId`**, nhưng ở bet request/response là `id` (hai DTO khác nhau theo thiết kế). |
| `options[].multiplier` | number | Hệ số nhân stake của gói này. `actualStake = baseStake × multiplier`. Client phải gửi lại đúng giá trị này trong bet request. |

> `multiplier` có thể bằng `1` trong paytable — đó là kích hoạt "không tính thêm phí"
> hợp lệ (distribution riêng vẫn được áp dụng).

### 2.2 `result.pending.premiumReel`

Snapshot của gói Premium Reel đang **active cho vòng Free Spin hiện tại**. Dùng để
khôi phục UI/state Premium Reel sau khi reload hoặc reconnect giữa lúc đang Free Spin.

```jsonc
{
  "result": {
    "pending": {
      "freeSpins": 4,
      "stake": 30,
      "gameState": 2,          // 2 = FreeGame
      "anteMulti": 0,
      "premiumReel": { "id": 2, "multiplier": 1.5 }
    }
  }
}
```

| Field | Kiểu | Ý nghĩa |
| --- | --- | --- |
| `pending.premiumReel` | object \| `null` | Snapshot của vòng FS. `null` khi người chơi **không** ở trong vòng free-spin Premium Reel — tức là đang ở BaseGame, ở free game của Promo, hoặc ở vòng free-spin thường (không premium). |
| `pending.premiumReel.id` | int | `reelId` được đóng băng lúc kích hoạt FS. **Ở đây key tên là `id`.** |
| `pending.premiumReel.multiplier` | number | Multiplier được đóng băng lúc kích hoạt FS. Không đổi dù paytable có thay đổi sau đó, trong suốt vòng chơi. |

**Quy tắc resume Free Spin:** khi `pending.premiumReel` khác `null`, **mọi** bet request
free-spin **bắt buộc** phải gửi lại `premiumReel` với đúng `id` và `multiplier` này.
Gửi id/multiplier khác, hoặc bỏ `premiumReel`, sẽ bị từ chối (`-111`). Xem §4.

### 2.3 `result.lastBet` — các field mới

Spin cuối cùng đã hoàn tất, dùng để replay khi rejoin.

| Field | Kiểu | Ý nghĩa |
| --- | --- | --- |
| `lastBet.premiumReelId` | int | Id gói của spin cuối. **`0`** (không phải `null`) khi spin cuối là spin thường. |
| `lastBet.premiumReelMultiplier` | number | Multiplier của spin cuối. `0` khi spin cuối là spin thường. |
| `lastBet.baseStake` | number | Số tiền cược người chơi chọn (số tiền dùng để tính payout). |
| `lastBet.stake` | number | Số tiền thực tế bị trừ (`baseStake × premiumReelMultiplier` với spin premium, ngược lại `= baseStake`). |

> `lastBet` dùng sentinel `0` (không bao giờ `null`) cho `premiumReelId` /
> `premiumReelMultiplier`, giống quy ước `anteMultiplier` đang có trên `lastBet`.
> Để xác định "spin cuối có phải spin premium không", kiểm tra
> `lastBet.premiumReelId !== 0`.

---

## 3. Bet request (`bet`) — field mới

Endpoint: `POST /api/slots/bet` (spin chính là `bet`; không có endpoint `spin`).
Bắt buộc có header `X-Idempotency-Key` như mọi bet.

### 3.1 Spin thường (không đổi)

```jsonc
{ "info": { "stake": 30, "betLines": 5 } }
```

### 3.2 Spin Premium Reel

```jsonc
{
  "info": {
    "stake": 30,
    "betLines": 5,
    "premiumReel": { "id": 2, "multiplier": 1.5 }
  }
}
```

```jsonc
// combo reel 2 + 3
{ "info": { "stake": 30, "betLines": 5, "premiumReel": { "id": 23, "multiplier": 2.2 } } }
```

| Field | Kiểu | Quy tắc |
| --- | --- | --- |
| `info.stake` | number | Là stake **gốc** (số tiền cược người chơi chọn). Server tự nhân — **không** nhân sẵn ở client. Vẫn phải là denomination hợp lệ như hiện tại. |
| `info.premiumReel` | object \| bỏ | Bỏ hẳn nếu là spin thường. `null` và bỏ là tương đương. |
| `info.premiumReel.id` | int | Phải là một trong các `reelId` từ `settings.premiumReel.options`. |
| `info.premiumReel.multiplier` | number | Phải **khớp chính xác** với `multiplier` được cấu hình cho `id` đó. Sai → từ chối (`-111`), không trừ tiền. |
| `info.anteMulti` | number | Phải là `0` / bỏ khi có `premiumReel`. Gửi cả hai → `-114`. |

### 3.3 Bet free-spin trong vòng Premium Reel

```jsonc
// stake 0, echo lại đúng snapshot pending.premiumReel
{ "info": { "stake": 0, "betLines": 5, "premiumReel": { "id": 2, "multiplier": 1.5 } } }
```

---

## 4. Bet response (`bet`) — các field mới

Object `result` trong response có thêm:

```jsonc
{
  "code": 0,
  "result": {
    "id": "9337...",
    "roundId": "9337...",
    "baseStake": 30,
    "stake": 45,
    "payout": 12.5,
    "winlose": -32.5,
    "gameState": 1,
    "premiumReelId": 2,
    "premiumReelMultiplier": 1.5,
    "freeSpins": 0,
    "result": { "views": [ /* reels */ ] }
    // ... các field cũ ...
  }
}
```

| Field | Kiểu | Ý nghĩa |
| --- | --- | --- |
| `result.baseStake` | number | Số tiền cược của người chơi. **Payline win được tính trên `baseStake`**, không phải trên `stake`. |
| `result.stake` | number | Số tiền đã bị trừ: `baseStake × premiumReelMultiplier` với spin premium; `= baseStake` với spin thường. |
| `result.winlose` | number | `payout − stake`. Với spin premium, phần thua tính trên số tiền **đã bị trừ**. |
| `result.premiumReelId` | int | Echo lại gói dùng ở spin này. **`0`** với spin thường. |
| `result.premiumReelMultiplier` | number | Echo lại multiplier. `0` với spin thường. |

Kỳ vọng theo từng phase:

| Phase | `baseStake` | `stake` | `premiumReelId` | `premiumReelMultiplier` |
| --- | --- | --- | --- | --- |
| Spin BaseGame thường | = `stake` | = `baseStake` | `0` | `0` |
| BaseGame — Premium Reel | tiền cược người chơi | `baseStake × mult` | từ request | từ request |
| FreeGame — vòng Premium Reel | tiền cược người chơi | `baseStake × mult` | từ snapshot FS | từ snapshot FS |
| FreeGame — vòng FS thường | = `stake` | = `baseStake` | `0` | `0` |

---

## 5. Validation & error code

Mọi từ chối xảy ra **trước khi** trừ tiền.

| Code | Tên | Khi nào |
| --- | --- | --- |
| `-111` | `PremiumReelMismatch` | `id` không có trong `options`; `multiplier` không khớp giá trị cấu hình cho `id` đó; **hoặc** trong vòng free-spin, `premiumReel` của request không khớp `pending.premiumReel` (sai id, sai multiplier, hoặc thiếu khi đang có snapshot). |
| `-112` | `PremiumReelNotSupported` | Gửi `premiumReel` nhưng tính năng bị tắt / không hỗ trợ (`settings.premiumReel` là `null`). |
| `-113` | `PremiumReelDuringPromo` | Gửi `premiumReel` trong spin promo bất kỳ (PromoGame hoặc free game trong promo). |
| `-114` | `PremiumReelAnteBetConflict` | Request có cả `anteMulti` (khác 0) và `premiumReel`. |
| `-115` | `PremiumReelDuringBuyBonus` | Gửi `premiumReel` khi đang trong luồng mua Buy Bonus. |
| `-116` | `PremiumReelInvalidRequest` | `premiumReel` sai định dạng (thiếu `multiplier`, sai kiểu, v.v.). |
| `-102` / `-106` | `InvalidStake` / `UnsupportedDenomination` | `stake` không hợp lệ — như hiện tại. |

---

## 6. Các quy tắc client bắt buộc tuân theo

1. **Đọc cấu hình từ `gameInfo`.** Không hardcode `reelId` hay multiplier.
   `settings.premiumReel == null` → game không có tính năng này, ẩn toàn bộ UI liên quan.

2. **Gửi `stake` là tiền cược gốc.** Server tự trừ `stake × multiplier`. Hiển thị
   "tổng chi phí" cho người chơi bằng cùng phép nhân đó, nhưng **gửi** giá trị chưa nhân.

3. **BaseGame: gói có thể đổi mỗi round.** Sau khi một spin đã settle, spin tiếp theo
   có thể chọn gói hợp lệ khác, hoặc không chọn. Không có khoá xuyên spin ở BaseGame.

4. **FreeGame: gói bị khoá.** Trong suốt vòng free-spin, client phải gửi lại
   `premiumReel` với đúng `id` + `multiplier` từ `pending.premiumReel` ở mọi bet
   free-spin. Không thể đổi hoặc bỏ giữa vòng.

5. **Loại trừ lẫn nhau (client enforce UI + server enforce).** Khi Premium Reel được
   chọn, disable Ante Bet và Buy Bonus. Server vẫn từ chối tổ hợp này dù sao, nhưng
   UI không nên cho phép chọn.

6. **Reload / reconnect.**
   - `pending.premiumReel != null` → người chơi đang ở giữa vòng free-spin Premium
     Reel; khôi phục state đó và dùng nó cho các bet resume.
   - `pending.premiumReel == null` và `pending.gameState == 1` (BaseGame) → không có
     vòng Premium Reel active; người chơi được tự do chọn gói cho spin tiếp theo.
   - Chỉ dùng `lastBet.premiumReelId` (`!= 0`) để replay hình ảnh của spin cuối, không
     dùng để xác định state hiện tại — ưu tiên `pending`.

7. **Free spin của Buy Bonus không bao giờ kế thừa Premium Reel.** Vòng FS của Buy
   Bonus có `pending.premiumReel == null`; bet free-spin trong đó không được gửi
   `premiumReel`.

8. **Idempotency không đổi.** Một `bet` trùng với cùng `X-Idempotency-Key` sẽ replay
   lại đúng kết quả cũ — một lần trừ tiền duy nhất, dù có hay không có `premiumReel`.
