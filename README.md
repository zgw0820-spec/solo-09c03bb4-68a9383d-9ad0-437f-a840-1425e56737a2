# 512e → 4Kn GPT 迁盘网页

上传 **512 字节逻辑扇区**的虚拟 RAW 数据盘，生成**长度完全相同**、供
**4 KiB 逻辑扇区**使用的 GPT 磁盘镜像。提供 FastAPI 网页、Docker Compose
启动方式，以及 5 个可直接在网页里点选的样盘场景。

## 快速开始

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

- 端口可用 `MIGRATE_PORT=9000 docker compose up` 覆盖。
- 上传上限、任务保留时间、并发数、分块大小均为环境变量（见下表）。
- 容器以 uid 10001 运行、只读根文件系统、`cap_drop: ALL`，任务数据放在
  `jobs` 命名卷的 `/data/jobs`。

### 本地（不用 Docker）

```bash
python3 -m pip install -r requirements-dev.txt
MIGRATE_WORK_DIR=./data/jobs MIGRATE_SAMPLE_DIR=./sample \
  python3 -m uvicorn app.main:app --port 8000
```

### 运行测试

```bash
python3 -m pytest -q
```

## 网页样盘

页面“网页样盘”区域有 5 个场景，点击即填入上传框（也可直接
`GET /api/samples/<key>` 下载）：

| key | 内容 | 预期 |
| --- | --- | --- |
| `healthy` | 16 MiB，双 GPT 一致，两个 4 KiB 对齐分区 | 转换成功 |
| `one-bad-copy` | 主 GPT 表头 CRC 被破坏，备 GPT 完好 | 用备份重建，成功 |
| `tail-collision` | 分区尾部伸入 4 KiB 备 GPT 保留区 | 拒绝（`GPT_REGION_CONFLICT`） |
| `unaligned` | 分区起点偏移 512 字节 | 拒绝（`PARTITION_NOT_ALIGNED`） |
| `both-bad` | 主备 GPT 均损坏 | 拒绝（`BOTH_GPT_INVALID`） |

`sample/sample-512b-gpt.img` 是提交在仓库里的 16 MiB 正常样盘，只有 GPT 区
与每分区一条横幅真正占用磁盘（稀疏文件实际占用约 48 KiB）。网页里
“正常样盘”直接使用该文件；其余 4 个场景在首次点击时按需生成到可写缓存目录
（`MIGRATE_SAMPLE_CACHE_DIR`，默认工作目录下 `_samples`）。可用
`python3 tools/make_sample.py` 重新生成并自检。

## 转换规则（实现即规格）

输入校验（任一不满足即停止并返回中文原因）：

1. 文件非空，长度是 512 的整数倍。
2. 保护 MBR 必须是单一 `0xEE` 项；出现其它类型或多个 `0xEE` 项即视为
   **混合 MBR**，拒绝。
3. GPT 分区表必须是 **128 项 × 128 字节**（16 KiB），否则两副本都判无效。
4. **主备 GPT 分别独立校验**：签名、HeaderCRC32、EntriesCRC32、MyLBA /
   AlternateLBA / EntryLBA 位置、可用区范围、磁盘 GUID。
   - 仅一份有效 → 用该份重建，并在结果页显示另一份的具体问题。
   - 两份都无效 → 拒绝（`BOTH_GPT_INVALID`）。
   - 两份都有效但**磁盘 GUID / 分区项 / 可用区不一致** → 拒绝
     （`GPT_COPIES_CONFLICT`），不做“以某一份为准”的猜测。
5. 分区语义合法：`FirstUsable ≤ start ≤ end ≤ LastUsable`，互不重叠，
   唯一 GUID 非空且不重复。
6. 4 KiB 前置条件：
   - 盘长是 4096 的整数倍（否则 `DISK_NOT_ALIGNED`）；
   - 每个分区的字节范围 `[start*512, (end+1)*512)` 两端都对齐 4096；
   - 新 4 KiB 布局的保留区不与任何分区相交：
     主 GPT 头部 `[0, 24576)`（MBR + 扇区 1 + 4 个分区项扇区），
     备 GPT 尾部 `[size-20480, size)`（4 个分区项扇区 + 表头）。
   - 重叠与未对齐问题会**一起列出**（`details.problems`），不会只报第一条。

重建：

- 保护 MBR：单一 `0xEE`，起始 LBA 1，大小 `min(盘长/4096 − 1, 0xFFFFFFFF)`。
- 主 GPT 表头 @ LBA 1，分区项 @ LBA 2..5；备 GPT 表头 @ 末 LBA，分区项紧邻其前。
- **保留**：磁盘 GUID、每个分区的类型 GUID、唯一 GUID、属性、名称（UTF-16LE）；
  分区 LBA 按 `字节 / 4096` 重新换算，字节范围与内容完全不变。
- 空分区项整 128 字节清零（源里类型为空但带残留字节的项会被报告并清理）。
- 分区内容逐字节复制，磁盘其余区域（含未分配空间、引导代码等）也逐字节复制。

成品复验（任一项失败即删除输出与清单、禁止下载）：

- 重新打开输出文件，独立解析主/备 GPT 与保护 MBR；
- 长度与源盘相等；两份 GPT 的 CRC、位置、可用区正确且互相一致；
- 分区表与重建结果逐字节一致，磁盘 GUID 未变；
- 每个分区的 **SHA-256 与源盘对应字节范围一致**（哈希在复制阶段顺带算出）；
- 保留区逐字节等于重建结果；
- 同一次顺序读盘顺带算出最终镜像每 4 MiB 的 SHA-256，用于生成分块校验清单
  （不整盘二次读、不另存整盘副本）。

## 分块校验清单与接收端复核

成品通过复验后，服务端同时生成并开放两份文件：

1. **4 KiB 镜像**（`<源名>.4k.img`）；
2. **分块校验清单**（`<源名>.4k.manifest.json`），记录最终镜像每 4 MiB 的
   SHA-256，块范围从字节 0 连续覆盖到盘尾，包括保护 MBR/主 GPT、分区内容、
   未分配区和不足 4 MiB 的盘尾块；另附 `regions` 把整盘切成
   `gpt-head / partition / unallocated / gpt-tail`，便于把不匹配块映射到
   需要重拷的区间。

复验和制单都成功后才开放**镜像、清单、离线校验页**三个下载；复验失败、
清单生成失败或发布前取消，镜像与清单一起撤回（任一缺失两个接口都返回 409）。

接收机上的复核流程：

1. 在迁盘结果页点“另存离线校验页”（或直接访问 `/api/verifier/offline.html`），
   得到一个不依赖服务器的单文件 HTML，连同镜像与清单拷到接收机；
2. 双击该 HTML（`file://`）打开，选择清单与本地镜像；
3. 页面用 `File.slice()` **按块读取本地文件，不上传**；长度与清单声明不符、
   或清单不完整（块数/连续性/覆盖范围不符）时直接拒绝核对并说明原因；
4. 长度相符时逐块比较 SHA-256，只列出**不匹配块的起始字节与长度**
   （附两侧哈希，可导出 CSV），值班员据此重拷对应区间。

## 内存占用

- 上传：4 MiB 分块写盘，不驻留内存。
- 复制：`MIGRATE_COPY_CHUNK_BYTES`（默认 4 MiB）分块 `pread`/`pwrite`，
  同时用 128 个 SHA-256 上下文按块喂入分区哈希。
- 复验 + 制单：同一次顺序读，按 `MIGRATE_COPY_CHUNK_BYTES` 读取，同时更新
  分区哈希与每 `MIGRATE_MANIFEST_BLOCK_BYTES`（默认 4 MiB）一个的分块哈希。
- GPT 解析/重建：只读 16 KiB 分区表与 1 个扇区表头。

因此峰值内存 ≈ 分块大小 × 常数 + 每分区哈希状态，**与盘长无关**。
测试 `test_copy_reads_are_bounded` 与 `test_verification_reads_the_image_once`
对此做了断言。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MIGRATE_WORK_DIR` | `/data/jobs` | 任务/成品目录 |
| `MIGRATE_SAMPLE_DIR` | `/srv/app/sample` | 预置样盘目录 |
| `MIGRATE_SAMPLE_CACHE_DIR` | `<工作目录>/_samples` | 缺省样盘的生成位置（需可写） |
| `MIGRATE_MAX_UPLOAD_BYTES` | `8589934592`（8 GiB） | 上传上限 |
| `MIGRATE_JOB_TTL_SECONDS` | `3600` | 成品保留时间，到期自动删除 |
| `MIGRATE_COPY_CHUNK_BYTES` | `4194304` | 复制/复验/哈希分块 |
| `MIGRATE_MANIFEST_BLOCK_BYTES` | `4194304` | 清单分块大小（4 MiB） |
| `MIGRATE_MAX_CONCURRENT_JOBS` | `1` | 转换工作线程数 |

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 网页 |
| POST | `/api/jobs` | 上传。支持 `multipart/form-data`（字段 `file`）或裸 `application/octet-stream`（文件名用 `X-Filename`） |
| GET | `/api/jobs/{id}` | 状态、进度、源盘报告、复验清单 |
| POST | `/api/jobs/{id}/cancel` | 取消 |
| DELETE | `/api/jobs/{id}` | 删除任务与文件 |
| GET | `/api/jobs/{id}/download` | 仅 `done` 且镜像与清单都就绪时可下载镜像 |
| GET | `/api/jobs/{id}/manifest` | 同上条件，下载分块校验清单 |
| GET | `/verify` | 在线打开离线校验页 |
| GET | `/api/verifier/offline.html` | 下载单文件离线校验页（内联 CSS/JS） |
| GET | `/api/samples` | 样盘目录 |
| GET | `/api/samples/{key}` | 下载样盘 |
| GET | `/api/config` | 前端配置 |
| GET | `/healthz` | 健康检查 |

错误统一为 `{"error": {"code", "message", "details"}}`，`message` 与
`details` 为中文说明。

实现细节、校验顺序与实测数据见 [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md)。

## 已知限制 / 待办

- 本机没有 docker/podman，**镜像未实际构建**；需在具备 Docker 的环境执行
  `docker compose up --build` 做冒烟（Dockerfile 会 `COPY app`，新增的
  `manifest.py`、`naming.py`、`templates/verify.html`、`static/verify.*`
  都在其中）。
- 只处理 **GPT**，不支持 MBR-only 磁盘；混合 MBR 明确拒绝而不是猜测。
- 要求标准 128×128 分区表与标准 GPT 位置；非标准（如仅 32 项、表项位置
  非标准）的盘会被判为无效副本。若主备都如此则拒绝转换。
- 分区名按 UTF-16LE 解码；含无效代理项时用替换字符显示，但原始字节保留。
- 单实例内存任务表：重启后未完成任务消失（目录会在 TTL 后清理），没有
  多副本共享的任务存储/队列。
- 下载为普通整文件响应（支持 Range 由 Starlette 决定），未做断点续传保证。
- 没有认证/限流；设计为内网工具，部署到公网前需加认证与配额。
- 尚未在真实 4Kn 硬件/虚拟机启动环境中验证引导，只做了镜像级校验。
- 离线校验页的纯 JS SHA-256 回退路径（非安全上下文）功能正确但速度较慢；
  `file://` 打开时使用 Web Crypto，不受影响。逐块核对需要浏览器一次读入
  一个 4 MiB 块，超大镜像仍需较长时间，但内存占用恒定。
- 清单为明文 JSON，未做签名。它能发现镜像损坏/拷错，但不能防御主动篡改
  清单本身；若需防篡改，应在带外核对页面显示的清单 SHA-256。
