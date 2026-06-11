# Team 12 — TransitFlow 資料庫設計文件

| 欄位 | 內容 |
|---|---|
| Team ID | 12 |
| 成員 | 張子衡 (113403062) · 劉亮廷 (113403541) · 蔡博宇 (113403056) |
| Repository | <https://github.com/VCTCHANG/Team12_113403062_transitflow> |
| 日期 | 2026-06-11 |

本文件說明 TransitFlow 助理背後三個資料庫的設計：**PostgreSQL**（關聯式主資料庫）、
**PostgreSQL + pgvector**（政策文件語意搜尋）、**Neo4j**（路網路徑運算）。
文中所有 schema 片段與查詢都直接取自 repository 內的實際程式碼。

---

## Section 1 — Entity-Relationship Diagram

![TransitFlow ER Diagram](https://raw.githubusercontent.com/VCTCHANG/Team12_113403062_transitflow/main/docs/TransitFlow_ER.png)

> 完整解析度版本在 repository 內：[`docs/TransitFlow_ER.png`](docs/TransitFlow_ER.png)（點陣圖）
> 與 [`docs/TransitFlow_ER.svg`](docs/TransitFlow_ER.svg)（向量圖，瀏覽器開啟可任意放大）。
> 此圖由程式直接從資料表定義生成，因此每個欄位名稱與型別都與
> [`databases/relational/schema.sql`](databases/relational/schema.sql) 完全同步。

### 圖例說明

* **實線**是真實的資料庫外鍵（FK）約束；**虛線**是由應用層維護的關聯
  （多型參照、自我參照、以及刻意不設 FK 的跨網路連結——理由分別在 Section 2 與
  Section 6 說明）。
* 基數（cardinality）**直接標在每一條線上**，且標了兩次：鳥爪端點符號
  （`‖` = 恰好 1、`|`+鳥爪 = 1..N、`○`+鳥爪 = 0..N、`|○` = 0..1），
  **加上** `1:N`、`1:1` 文字標籤。最小基數 1 屬於業務規則、由應用層保證
  （例如每條班次至少有一個停靠站）——因為 FK 本身無法對父端強制最小數量。
* 每個實體框列出完整欄位、PK / FK / UK 標記與精確的 PostgreSQL 型別。

### 實體清單（共 16 個）

| 分群 | 實體 |
|---|---|
| 捷運路網 | `metro_stations`、`metro_schedules`、`metro_schedule_stops`、`metro_schedule_operates_on` |
| 鐵路路網 | `national_rail_stations`、`national_rail_schedules`、`national_rail_schedule_stops`、`national_rail_schedule_operates_on`、`seat_layouts` |
| 使用者與帳務 | `users`、`user_credentials`、`payments`、`feedback` |
| 訂票與旅程 | `national_rail_bookings`、`metro_travels` |
| RAG（獨立） | `policy_documents` |

### 主要關聯一覽

| 關聯 | 基數 | 由誰保證 |
|---|---|---|
| `users` — `user_credentials` | 1 : 1 | `user_credentials.user_id` 同時是 PK 與 FK |
| `users` — `national_rail_bookings` / `metro_travels` / `feedback` | 1 : 0..N | FK `ON DELETE RESTRICT` |
| `*_schedules` — `*_schedule_stops` | 1 : 1..N | FK `ON DELETE CASCADE`；junction table 解開 stations ↔ schedules 的 M:N |
| `*_stations` — `*_schedule_stops` | 1 : 0..N | FK `ON DELETE RESTRICT` |
| `*_schedules` — `*_schedule_operates_on` | 1 : 1..N | FK `ON DELETE CASCADE` |
| `national_rail_schedules` — `seat_layouts` | 1 : 0..N | FK `ON DELETE CASCADE` |
| `*_stations` — `*_schedules`（origin / destination） | 1 : 0..N（兩條 FK） | FK `ON DELETE RESTRICT` |
| `national_rail_bookings` / `metro_travels` — `payments`、`feedback` | 1 : 0..N | 應用層（多型 `booking_id`，見 Section 2） |
| `metro_travels` — `metro_travels`（`day_pass_ref`） | 0..1 : 0..N | 應用層（自我參照） |
| `metro_stations` — `national_rail_stations`（interchange） | 0..1 : 0..1 | 應用層（跨網路，不設 FK 以避免循環相依） |

---

## Section 2 — Normalisation Justification

### 2.1 一個真實的 3NF 決策：停靠站序列拆成 junction table

原始的 `metro_schedules.json` 用「有序陣列＋對照表」存每條班次的路線：

```json
"stops_in_order": ["MS20", "MS05", "MS01", ...],
"travel_time_from_origin_min": {"MS20": 0, "MS05": 2, "MS01": 5, ...}
```

我們第一版的實作直接把這個形狀搬進資料庫，在 `metro_schedules` 上開兩個 `JSONB`
欄位。這個設計違反**第一正規化（1NF）**——欄位值不是原子值，而是容器——而且它掩蓋了
一個**功能相依（functional dependency）**：停靠順序與行車時間是由
（`schedule_id`, `station_id`）這個**組合**決定的，不是由 `schedule_id` 單獨決定。
把它們塞在班次列裡，也讓最常用的查詢（「這條班次是否先停 A 再停 B？」）每次都得
展開 JSON 陣列。

因此我們把停靠序列正規化成 junction table：

```sql
CREATE TABLE IF NOT EXISTS metro_schedule_stops (
    schedule_id             VARCHAR(20)  NOT NULL REFERENCES metro_schedules(schedule_id) ON DELETE CASCADE,
    station_id              VARCHAR(10)  NOT NULL REFERENCES metro_stations(station_id) ON DELETE RESTRICT,
    stop_order              INTEGER      NOT NULL,  -- 路線中的位置（從 0 起算）
    travel_time_from_origin INTEGER      NOT NULL,  -- 距首站的分鐘數
    PRIMARY KEY (schedule_id, station_id)
);
```

這張表符合 **3NF**：複合主鍵（`schedule_id`, `station_id`）是唯一的候選鍵
（candidate key）；`stop_order` 與 `travel_time_from_origin` 相依於**整個**鍵
（沒有部分相依，故滿足 2NF），且只相依於鍵本身（沒有遞移相依，故滿足 3NF）。
它同時解開了 JSON 形狀掩蓋住的 stations ↔ schedules 多對多關係。
「方向正確的 A 在 B 之前」從此變成一個單純的自我 join：

```sql
FROM national_rail_schedules s
JOIN national_rail_schedule_stops o ON o.schedule_id = s.schedule_id AND o.station_id = %s
JOIN national_rail_schedule_stops d ON d.schedule_id = s.schedule_id AND d.station_id = %s
WHERE o.stop_order < d.stop_order
```

同樣的推理產生了 `*_schedule_operates_on`（一個營運日一列）：`operates_on` 原本是
重複群組 `["mon","tue",...]`，攤平之後，「查某天有沒有開」用一個簡單的 `EXISTS`
就能過濾，不必做 JSON 包含測試。

### 2.2 刻意保留的反正規化取捨

我們**沒有**把所有東西都正規化到底；以下三個選擇是有意識地用冗餘換取查詢簡潔：

1. **票價艙等用欄位、不用子表。** `national_rail_schedules` 直接存
   `std_base_fare_usd`、`std_per_stop_rate_usd`、`first_base_fare_usd`、
   `first_per_stop_rate_usd` 四個欄位，而不是正規化成
   `fares(schedule_id, fare_class, base, rate)` 子表。艙等恰好只有兩種、由業務規則
   固定，而且每次票價查詢都需要兩種一起比較——開子表只會讓每個可用性／票價查詢多
   一次 join（或 pivot），完整性上毫無收益。
2. **`seat_layouts` 裡一個被接受的遞移相依。** 原始資料中車廂決定艙等，嚴格來說
   `seat → coach → fare_class` 是遞移相依（transitive dependency），3NF 應該再拆一張
   `coaches` 表。我們把 `fare_class` 留在座位列上，因為 `query_available_seats`
   直接用艙等過濾座位；這份冗餘在 seed 時寫入一次、之後永不更新，所以經典的
   更新異常（update anomaly）風險實際上不存在。
3. **車站的 `lines` 用 JSONB。** 車站的路線清單（如 `["M1","M2"]`）純粹是顯示用
   的中繼資料。真正需要完整性的路網拓撲存在 Neo4j 和停靠站 junction table 裡，
   再開一張 `station_lines` junction 是沒有使用者的正規化。

另一個相關決策是**多型的 `payments.booking_id`**：它可能指向
`national_rail_bookings`（`BK…`）或 `metro_travels`（`MT…`）。PostgreSQL 無法宣告
「指向 A 表或 B 表」的 FK，所以我們保留單一 payments 表（金流的單一事實來源），
參照完整性改在訂票／取消的交易程式裡保證。替代方案——兩張付款表、或開超型別
（supertype）表——前者會把財務報表切成兩半，後者讓每次付款查詢多一個 join。

### 2.3 密碼（與安全問題答案）的儲存

**演算法。** 密碼在 `register_user()` 註冊時以 **bcrypt** 雜湊
（`bcrypt.hashpw`，cost factor 12），登入時在 `login_user()` 用 `bcrypt.checkpw`
驗證。任何長得像密碼的東西都不曾以明碼儲存或比對。

**為什麼是 bcrypt 而不是 MD5 / SHA-1。** MD5 與 SHA-1 是**快速**的通用雜湊——
一張 GPU 每秒能算數十億次，所以即使加了 salt 的 MD5 也很快被暴力破解，而且兩者
都有已知的碰撞攻擊。bcrypt 是**刻意設計成慢**的密碼雜湊函數，核心是 key
stretching：它的 cost factor（我們用 2¹² 輪）讓攻擊者每猜一次都貴上數千倍，而且
cost 存在雜湊字串裡，未來可以調高而不破壞既有雜湊。慢，對一次登入無感，
對要嘗試數百萬組候選密碼的攻擊者卻是毀滅性的。

**salt 怎麼管理、為什麼能擋彩虹表。** `bcrypt.gensalt()` 為每個使用者產生獨一無二
的隨機 salt，並混入雜湊。彩虹表（rainbow table）是預先算好的「雜湊 → 密碼」
查找表：它成立的前提是同一個密碼永遠產生同一個雜湊。有了每人不同的 salt，
兩個都用 `password123` 的使用者會存下完全不同的值——例如
`$2b$12$N9qo8uLO…` 和 `$2b$12$R4fJ9aKp…`——預算表因此完全失效，攻擊者被迫
以 bcrypt 的速度對每個帳號單獨暴力破解。bcrypt 的雜湊字串本身已內含 salt；
我們額外存一個明確的 `salt` 欄位是為了可稽核性（審查者用一條查詢就能確認
每人 salt 皆不相同）。

**與個人資料分離。** 所有憑證資料都放在 `user_credentials`——一張與 `users` 1:1 的
獨立表（`user_id` 同時為 PK 與 FK，`ON DELETE CASCADE`）。只需要個人資料
（姓名、email）的查詢永遠不會碰到憑證——這是最小權限原則落實在 schema 層。
`email` 在 `users` 上維持**候選鍵**身分（宣告 `UNIQUE`），且儲存與查詢前都先
正規化為小寫，因為 `VARCHAR` 比對是區分大小寫的。

**安全問題的答案也是憑證。** 專案後期我們發現 `secret_answer` 以明碼躺在
`users` 表裡。答對它就能重設密碼——這讓它在功能上等同備用密碼，明碼存放等於在
上述所有防護旁邊開了一道後門。我們把它搬到
`user_credentials.secret_answer_hash`，先正規化（`strip().lower()`）再以 bcrypt
雜湊。這個正規化保住了規格要求的不分大小寫比對：把使用者輸入做同樣的正規化，
再 `checkpw` 對雜湊即可，全程不存明碼。

---

## Section 3 — Graph Database Design Rationale

### 3.1 什麼當節點、什麼當關係、什麼當屬性——以及為什麼

**節點 = 車站**（20 個 `MetroStation` ＋ 10 個 `NationalRailStation`）。車站是
旅程「經過」的東西；路徑運算就是在它們之上的遍歷。我們用兩個不同的 label，
而不是單一 `Station` label 加 `network` 屬性，因為 label 過濾是 Cypher 裡最便宜的
述詞（`MATCH (s:MetroStation)` 完全不用讀屬性），而且兩個網路的營運規則
（票價、訂位）確實不同——這個分離忠實對應領域本身。

**關係 = 實體連結**（42 條 `METRO_LINK`、18 條 `RAIL_LINK`、3 組雙向
`INTERCHANGE_TO`）。來源 JSON 的每筆相鄰關係成為一條有向關係；因為列車雙向行駛，
連結兩個方向都存在。不同網路共站的步行轉乘自成一個關係型別 `INTERCHANGE_TO`，
讓跨網路路徑變成**可選項**：應該留在單一網路內的查詢，只要在 relationship filter
裡不放這個型別即可。

**屬性 = 放在關係上的遍歷成本。** `travel_time_min`（來自原始資料）、`fare` 與
`fare_first`（每段的票價權重）、`line` 都放在關係上而非節點上，因為成本屬於
**路段**：A 到 B 的時間是那條連結的屬性，而 Dijkstra 在展開時原生讀取邊權重。
轉乘邊帶 `travel_time_min: 5, fare: 0`——換網路花時間、不花錢。節點屬性
（`station_id`、`name`、`lines`）則是身分與顯示資料。

### 3.2 為什麼這類工作負載用圖資料庫勝過 SQL

路徑運算是**遞移閉包（transitive closure）**問題：答案是一條**長度未知**的路。
SQL 要靠遞迴 CTE，而一個正確的遞迴 CTE 必須：(a) 在每一列攜帶一個不斷累積的
「已造訪車站」陣列，純粹為了防環；(b) 物化**每一條**部分路徑，因為集合導向的
求值沒有「先展開最有希望的路」這種概念；(c) 每加深一層遞迴就把整張邊表再 join
一次。它沒有目標導向、也無法提前終止——資料庫在把所有更短的路都展開完之前，
無法知道自己已經找到了到 NR05 的最佳路線。

Neo4j 把相鄰關係存在記錄本身（index-free adjacency）：展開一個節點的鄰居是
指標跳轉，不是索引查找或 join。在這之上，`apoc.algo.dijkstra` 跑的是真正的
Dijkstra 演算法——以累積權重排序的 priority queue，複雜度 `O((V+E) log V)`，
目標節點一旦定案就立刻終止——而 `shortestPath()` 提供目標導向的 BFS 來回答
跳數問題。我們的路網很小（30 個節點／66 條有向邊），但這個不對稱是結構性的：
SQL 每多一跳就要對整個邊集合多做一次自我 join，圖資料庫永遠只碰它正在展開的
前沿。

### 3.3 圖模型讓哪些查詢變得自然（兩種以上）

**（1）加權最快路徑——以 `travel_time_min` 跑 Dijkstra**
（`databases/graph/queries.py` 的 `query_shortest_route`）：

```cypher
MATCH (a {station_id: $o}), (b {station_id: $d})
CALL apoc.algo.dijkstra(a, b, 'METRO_LINK|RAIL_LINK|INTERCHANGE_TO', 'travel_time_min')
YIELD path, weight
RETURN path, weight ORDER BY weight LIMIT 1
```

relationship filter 本身**就是**網路政策：傳 `'METRO_LINK'` 限捷運、
`'RAIL_LINK'` 限鐵路、傳聯集則全開放。同一個函式把權重屬性換成 `fare` 或
`fare_first` 就變成 `query_cheapest_route`——這正是成本要放在關係上的原因。
「換一個最佳化指標」在 SQL 裡等於重寫整個遞迴 CTE。

**（2）跨網路轉乘路徑**（`query_interchange_path`）：因為轉乘是獨立的關係型別，
捷運→鐵路的旅程就是**同一個** Dijkstra 呼叫、把 `INTERCHANGE_TO` 加進 filter；
之後對路徑的每一段讀 `type(r)`，就能回報乘客確切在哪裡換網路。路徑只有在值得
那 5 分鐘時才會跨網——模型把這個決策「定價」了，而不是寫特例。

**（3）誤點漣漪**（`query_delay_ripple`）：「哪些車站在受影響站的 N 跳以內」
是一個變長 pattern 加路徑函數：

```cypher
MATCH (s {station_id: $id})
MATCH (n)
MATCH sp = shortestPath((s)-[:METRO_LINK|RAIL_LINK|INTERCHANGE_TO*0..2]-(n))
RETURN n.station_id, n.name, length(sp) AS hops_away
```

`length(sp)` 直接**就是** `hops_away` 的答案；`*0..N` 的下界 0 讓 `hops = 0`
正確地只回傳受影響車站自己。SQL 的等價物又是一個要手動追蹤深度的遞迴 CTE。

### 3.4 節點身分（node identity）

節點以 **`station_id`** 屬性識別（`MS01`–`MS20`、`NR01`–`NR10`）——與關聯式 PK
相同的營運商自然鍵，這讓跨資料庫銜接毫無成本（agent 把 ID 在 PostgreSQL 結果與
Cypher 參數之間原封不動地傳遞）。它在各網路內保證唯一、穩定、且人類可讀。
`name` 不適合當身分：沒有任何機制保證唯一，而顯示名稱可能改變。所有 seeding 都用
`MERGE (n:MetroStation {station_id: $id})`，因此 seeder 是冪等的——重跑會更新屬性
而不是長出重複節點，關係的 `MERGE` 也錨定在同一組身分上。

---

## Section 4 — Vector / RAG Design

### 4.1 嵌入了什麼，以及為什麼用餘弦相似度

我們嵌入的是**政策知識庫**：來自 `refund_policy.json`、`ticket_types.json`、
`booking_rules.json`、`travel_policies.json` 的 17 份文件（退費窗口、票種規則、
行李／單車／寵物政策……）。每一條目成為 `policy_documents` 的一列，帶著它的
條文內容，以及由「之後也會拿來嵌入使用者問題的同一個模型」產生的 768 維向量。

相似度用**餘弦相似度（cosine similarity）**衡量，而這個選擇是有理由的：餘弦比較
的是兩個向量之間的**夾角**，忽略**長度（magnitude）**。嵌入向量的範數會隨表面
特徵變動——更長、更密的文件傾向產生範數更大的向量——而*語意*編碼在向量於嵌入
空間中的方向上。若用內積或歐氏距離排序，一份冗長的政策可能僅僅因為向量比較長
就壓過一份簡短但正中主題的政策；餘弦把這個因素正規化掉了，所以
「誤點可以拿回錢嗎？」即使和文件條文幾乎沒有共同關鍵字，也會把誤點賠償政策排
在第一。索引就是為這個度量建的：

```sql
CREATE INDEX IF NOT EXISTS policy_documents_embedding_idx
    ON policy_documents USING hnsw (embedding vector_cosine_ops);
```

HNSW 是近似最近鄰圖索引，知識庫成長時檢索仍維持次線性。

### 4.2 完整的 RAG 管線

1. **問題嵌入。** 使用者的問題經 `llm.embed(question)` 嵌入——Ollama 的
   `nomic-embed-text`，輸出 768 維向量。關鍵是這與 seed 時用的是*同一個模型*：
   問題與文件必須活在同一個嵌入空間，距離才有意義。
2. **相似度搜尋。** 執行 `query_policy_vector_search()`，其中 `<=>` 是 pgvector
   的餘弦距離運算子（相似度 = 1 − 距離）：

   ```sql
   SELECT title, category, content,
          1 - (embedding <=> %s::vector) AS similarity
   FROM policy_documents
   WHERE 1 - (embedding <=> %s::vector) > 0.5      -- 相關性門檻
   ORDER BY embedding <=> %s::vector
   LIMIT 3;                                        -- top-k
   ```

   0.5 的門檻避免在知識庫裡其實沒有東西相關時，硬把不相關的文件填進上下文。
3. **檢索結果 → 提示詞。** 前 3 列由 agent 的正規化器攤平成純文字區塊
   （標題、類別、內容、相似度分數），與原始問題一起注入 LLM 的 prompt。
4. **生成答案。** LLM *根據檢索到的條文*作答——例如直接引用退費窗口 RF005 的
   百分比，而不是自己編。檢索讓生成有所本；模型提供的是措辭，不是事實。

### 4.3 嵌入維度的選擇，以及換 provider 會發生什麼

我們的實作使用 **768 維**——Ollama `nomic-embed-text` 的輸出大小——欄位宣告為
`embedding vector(768)`。Gemini 的 `gemini-embedding-001` 則輸出 **3072 維**。

維度同時烙在 schema 裡*和*每一個已儲存的向量裡，所以 seed 之後切換 provider 會在
兩個層面破壞檢索。第一層：pgvector 拒絕比較不同長度的向量——拿 3072 維的問題
向量去比 768 維的資料列會直接拋出維度不符（dimension mismatch）錯誤，於是每一個
政策問題都報錯。第二層：就算把欄位改了大小，舊向量也已毫無意義——兩個模型定義
的是*不同的嵌入空間*，跨空間逐座標比較得到的是雜訊，不是相似度。唯一正確的復原
方式是：把 schema 改成 `vector(3072)`、清掉資料庫
（`docker compose down -v && docker compose up -d`）、再用新 provider 重新嵌入全部
文件（`python skeleton/seed_vectors.py`）。正因如此，我們的團隊規則是在任何人執行
seed **之前**先在 `.env` 統一 provider（Ollama）——不同組員用不同 provider 寫入的
向量會以無聲的方式互不相容。

---

## Section 5 — AI Tool Usage Evidence

整個專案我們都搭配 AI 助理使用（主要是 Claude，快速查證用 ChatGPT）。以下四個
代表性案例，其中包含一個 AI 輸出錯誤、由我們發現並修正的案例。

### 案例 1 — Schema 設計：AI 推薦的 JSONB 方案對我們是錯的

* **Context（情境）：** 為 `metro_schedules.json` 設計資料表。每條班次帶一個有序的
  `stops_in_order` 陣列和一個 `travel_time_from_origin_min` 對照表。
* **Prompt（提示詞）：** 「幫這份班次 JSON 設計 PostgreSQL 資料表（附樣本）。每條
  班次有一個有序的停靠站清單和一個以車站為鍵的行車時間表。停靠站用什麼方式存
  最好？」
* **Outcome（結果）：** AI 很有自信地建議把兩個欄位都留成 `JSONB`、用
  `jsonb_array_elements_text()` 查詢，理由是關聯式資料表不擅長處理有序陣列。我們
  採用了，也確實能跑——但在撰寫本文件的正規化章節時，我們意識到這個設計違反
  1NF（非原子值），並且埋掉了（`schedule_id`, `station_id`）→（`stop_order`,
  `travel_time`）這個功能相依。我們回頭質問 AI「用 3NF 批判你自己的 JSONB 設計」，
  它立刻翻轉立場，產出了我們現在使用的 junction table 設計
  （`*_schedule_stops`、`*_schedule_operates_on`）。git 歷史記錄了兩個狀態
  （commit `48348f2` JSONB 版 → commit `69b55bf` junction table 版）。
  **教訓：** 除非把正規化明確放進問題裡，AI 預設最佳化的是實作方便，不是正規形式。

### 案例 2 — 查詢撰寫：分方向的可用性查詢

* **Context：** 實作 `query_national_rail_availability`——只能回傳「先停起站、
  後停迄站」的班次，並附上即時剩餘座位數。
* **Prompt：** 「給定 national_rail_schedules 和 national_rail_schedule_stops
  （schedule_id, station_id, stop_order）兩張表，寫一個查詢回傳同時停靠 :origin 與
  :dest 且順序正確的班次，外加 seat_layouts 扣掉 :date 當天未取消訂位的可用座位
  數。」
* **Outcome：** AI 給出了我們最終保留的雙重 join 寫法（stops 表 join 兩次、
  `WHERE o.stop_order < d.stop_order`）——比我們原本「撈出停靠清單後在 Python 裡
  比位置」的草稿乾淨。但最終測試時我們發現一個*我們和 AI 都*沒涵蓋的缺口：
  NR_SCH05–08 只在平日營運，查週六卻照樣回傳。我們補上對
  `national_rail_schedule_operates_on` 的 `EXISTS` 過濾，用
  `to_char(%s::date, 'dy')` 比對星期。AI 加速了正確的 join 結構；領域的邊界案例
  仍然得靠我們自己讀資料。

### 案例 3 — 除錯：email 大小寫 bug

* **Context：** 交件前總體檢。我們請 AI 審查整條註冊 → 登入 → 訂票 → 取消流程，
  找出評分者可能觸發的 bug。
* **Prompt：** 「這是 ui.py 和 queries.py。追蹤註冊與登入的資料流，列出任何會讓
  後續查詢失敗的輸入。特別注意『存進去的』和『拿來比對的』是不是同一個東西。」
* **Outcome：** 它找到一個我們從未踩到的真 bug：`do_register` 放進 session state 的
  email 轉了小寫，但 `register_user` 存進資料庫的是原始大小寫。任何用
  `Alice@Example.com` 註冊的使用者登入沒問題，但 `query_user_bookings` /
  `make_booking` 會查無此人——因為 `VARCHAR` 比對區分大小寫。我們從沒發現是因為
  測試時永遠打小寫。修法：一個 `_norm_email()` 輔助函式（`strip().lower()`）
  同時套在儲存*和*每一處查詢上，然後用「混合大小寫註冊、再用不同大小寫登入」
  做回歸測試。

### 案例 4 — 設計依據：把安全問題的答案當成憑證

* **Context：** 課程要求密碼不得明碼、不得存在 user table。但我們的 `users` 表裡
  還有明碼的 `secret_answer`。
* **Prompt：** 「我們的 users 表有 secret_question 和 secret_answer 欄位；答對答案
  可以重設密碼。把它們存在這裡是安全問題嗎？要怎麼修，又不破壞規格要求的不分
  大小寫比對？」
* **Outcome：** AI 的分析：secret *question* 本來就是公開的（任何人輸入 email 都會
  看到它），可以留下；secret *answer* 在功能上是備用密碼——明碼存放等於繞過
  bcrypt 的帳號接管後門。它提出、我們實作的修法：搬到
  `user_credentials.secret_answer_hash`，雜湊**之前**先正規化（`strip().lower()`），
  驗證時把使用者輸入做同樣的正規化再 `bcrypt.checkpw`——保留不分大小寫的比對，
  全程不儲存明碼答案。我們同步遷移了 schema、seeder 與查詢，事後完整驗證了
  忘記密碼流程。

---

## Section 6 — Reflection & Trade-offs

### 兩個具體設計決策與理由

**1. 自然 `VARCHAR` 鍵，而不是 `SERIAL` 或 `UUID`。** 每個核心實體都以原始資料中
營運商指派的代碼為鍵（`MS01`、`NR_SCH01`、`RU01`、`BK001`）。不選 `SERIAL` 是因為
代碼已經存在且由營運商保證唯一——再加一個自動遞增代理鍵會迫使每個 seeder 和每次
跨資料庫呼叫都維護一張轉換表，而圖資料庫終究還是需要自然代碼（Neo4j 的節點身分，
見 Section 3.4）。不選 `UUID` 是因為我們是單區域、單寫入者的系統，分散式 ID 生成
毫無用武之地，卻要犧牲日誌與票據的可讀性（確認訊息裡的 `BK-A1B2C3` 遠勝 36 字元
的 UUID）。接受的代價：`VARCHAR` join 比 `INTEGER` join 略慢，在我們的資料量下
無關緊要。

**2. 全面軟刪除，搭配 `ON DELETE RESTRICT`。** 使用者停用（`is_active = FALSE`）、
訂票取消（`status = 'cancelled'`）——任何涉及金錢的紀錄都不做物理刪除。如果從
`users` 硬 `DELETE ... CASCADE`，會連環摧毀訂票並讓付款變成孤兒——也就是摧毀
「我們收過錢」的稽核軌跡。schema 把這個政策變成強制：訂票指向使用者與車站的 FK
都是 `RESTRICT`（有財務子紀錄的父列刪不掉），真正附屬的資料（憑證、班次的停靠站）
才 `CASCADE`。代價是每個「現役」查詢都要過濾（`status != 'cancelled'`、
`is_active`），我們有意識地付這個成本——例如 `login_user` 明確拒絕已停用帳號。

### 上線環境會有什麼不同

**Schema 遷移。** 我們改 schema 的流程是
`docker compose down -v && docker compose up -d` 加全量重新 seed——直接把資料庫
扔掉。當所有資料都是可重生的 mock data 時這沒問題；在生產環境不可想像，因為資料
本身*就是*產品。真實部署會用遷移工具（Alembic、Flyway）：每次變更成為一支有版本、
有順序、經過 review 的遷移腳本（`003_add_secret_answer_hash.sql`），增量套用、
不丟資料、附帶測過的回滾路徑。我們的 secret answer 修正就是具體例子：在本專案是
「改 `schema.sql`、清庫、重 seed」；在生產環境必須是線上遷移——先加可空欄位、
分批回填雜湊、部署改讀新欄位的程式碼、最後才移除 `users.secret_answer`——每一步
都可回退。其次，我們的 `_connect()` 每次查詢都開新的 psycopg2 連線，正確但浪費；
生產環境會在 PostgreSQL 前面放 PgBouncer 或應用端連線池，憑證也會改由密鑰管理
服務提供，而不是 `.env` 檔。
