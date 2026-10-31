# astrbot_plugin_ace_singing (v1.10.2)

专注原声翻唱的 AstrBot 插件：**QQ 音乐搜歌 + Seed-VC V1 (44.1kHz) 神经歌声转换 + 智能乐句起唱吸附 + 人格专属翻唱声线 + 推理进度回传 + 全参数即时热更新**。

> **架构分工说明**：
> - **翻唱能力**（原曲原旋律换音色）：由本插件 `astrbot_plugin_ace_singing` 负责（命令 `/点唱`、工具 `cover_a_song`）。
> - **日常说话配音**：交给 AstrBot **内置 TTS 管线**（在配置档中按概率自动合成语音）。
> - **大模型主动发语音**：交给 `astrbot_plugin_actively_calls_tts` 插件的 `actively_send_voice_message` 工具。
> - **后端服务**：翻唱 / TTS / ASR 统一由本地 [audio-station](https://github.com/luoxuanwen/audio-station) 提供（单进程单端口）。

---

## 一、 功能亮点

1. **原曲原调原声翻唱（只换音色）**：深度兼容 `qqmusic-api-python 0.8.2` 引擎，支持 320k/128k 高保真音源流，经由 Seed-VC 保持原曲旋律与伴奏、只把人声音色换成目标人格，混音后通过 QQ 原生语音发送。
2. **音色区分度增强**：音色库短人声样本直接作为干净参考（不二次分离，避免损伤音色）；CFG（inference_cfg_rate）默认提到 0.95（Seed-VC 安全上限），最大化向参考音色贴合；扩散步数默认 50，重建更干净、音色更稳——解决“换哪个音色都差不多”的问题。
3. **人声乐句级高潮定位**：优先解析 QQ 音乐带时间戳歌词（LRC）定位重复副歌段，保证截点落在人声乐句边界；无歌词时回退 RMS 能量包络找最响高潮段并吸附人声起止谷点，配合 0.6s 淡入与 2.2s 平滑渐隐，杜绝截到前奏/间奏纯音乐。
4. **人格-翻唱音色自动切换**：根据当前会话绑定的 AstrBot 人格名称（如「猫娘」），自动路由至专属翻唱声线；人格名支持精确/忽略大小写/子串三级匹配。未配置映射的人格继承兜底音色（默认 `112`）。
5. **全链路容错与风控保护**：遇到 VIP 歌曲或账号风控时自动在聊天窗口下发 QQ 音乐登录二维码；网络请求与网关探测带自愈及降级重试。
6. **智能音域抗破音**：按目标音色音域自适应变调，避免副歌高音段转音破音。

---

## 二、 指令与工具

| 触发方式 | 调用语法 / 示例 | 说明 |
| :--- | :--- | :--- |
| **高潮点唱（默认）** | `/点唱 <歌名>`（如 `/点唱 春三月`） | 默认从高潮段起唱约 36 秒（优先按歌词乐句定位，无歌词按能量高潮定位） |
| **全曲完整翻唱** | `/点唱 <歌名> 完整版` | 识别为整首翻唱，不作任何截断 |
| **LLM 自然点歌** | 对话中说："*唱一首春三月*" | 大模型自动调用 `cover_a_song` 工具并发送原生语音 |
| **服务诊断测试** | `/服务测试`、`/唱歌测试` | 实时测试翻唱服务 HTTP 连通与可用音色列表 |
| **状态查看** | `/唱歌状态` | 查看翻唱配置快照、人格映射表及网络链路状态 |
| **QQ 音乐登录** | `/qq登录` | 弹出扫码登录二维码（遇 VIP 歌或风控失效时也会自动弹出） |

---

## 三、 配置项说明 (`_conf_schema.json`)

| 配置项 | 默认值 | 说明 |
| :--- | :--- | :--- |
| **`cover_provider`** | `music` | 【翻唱提供商】：下拉选择在 AstrBot「模型提供商」中配置的翻唱服务 |
| **`svc_voice_profile`** | `112` | 【兜底翻唱音色】：默认翻唱声线（可选 `112`、`11`、`galgame`、`galgame2` 等） |
| **`persona_cover_map`** | `{}` | 【人格-翻唱音色映射】：为人格指定专属翻唱声线（只影响翻唱） |
| **`svc_segment_sec`** | `36` | 【副歌时长(秒)】：默认翻唱高潮秒数（按歌词乐句/能量谷点自动微调收尾） |
| **`svc_n_step`** | `32` | 【扩散步数】：Seed-VC 扩散推理步数（推荐 16~50） |
| **`svc_cfg`** | `0.7` | 【CFG】：Seed-VC inference_cfg_rate（0.1~0.95，越大音色越贴近参考音色，过高会失真） |
| **`svc_seed`** | `-1` | 【种子】：`-1` 为每次随机，`>=0` 启用固定种子/缓存 |
| **`svc_vocal_gain`** | `1.15` | 【人声增益】：换声后人声增益 |
| **`svc_instr_gain`** | `0.85` | 【伴奏增益】：原曲原声伴奏增益 |
| **`timeout_sec`** | `600` | 单次翻唱推理与网络请求超时上限（秒） |

---

## 四、 人格-翻唱音色映射 (`persona_cover_map`) 填法

在 WebUI 插件设置中填入文本框（**修改后即刻生效，无需重启容器**）：

**写法 1（推荐 JSON）：**
```json
{
  "猫娘": "112",
  "温柔御姐": "galgame2"
}
```

**写法 2（结构化）：**
```json
{
  "猫娘": { "cover_voice": "112" },
  "温柔御姐": { "cover_voice": "galgame2" }
}
```

**写法 3（纯多行文本）：**
```text
猫娘: 112
温柔御姐: galgame2
```

---

## 五、 后端部署（audio-station）

本插件依赖本地音频后端 **audio-station**（单进程单端口，聚合 TTS / ASR / 翻唱）：

- 仓库：<https://github.com/luoxuanwen/audio-station>
- 部署步骤、模型权重下载、显存要求与常见坑，见该仓库 README
- 部署完成后，AstrBot 侧三个提供商全部指向同一地址（audio-station 默认监听端口 `18000`，经 frp 穿透时替换为远端端口）：

| AstrBot 配置项 | 值 |
| :--- | :--- |
| TTS 提供商（OpenAI TTS/API）Base URL | `http://<host>:<port>/v1`，model `breeze-tts-2`，voice 填克隆音色名 |
| ASR 提供商（OpenAI ASR/API）Base URL | 同上 |
| 本插件「翻唱提供商」Base URL | 同上，模型 ID `seed-svc` |

> 插件在未配置翻唱提供商时兜底使用 `http://127.0.0.1:18000`（audio-station 默认端口）。翻唱的进度回传走 Gradio `/convert` 进度流，因此 Base URL 需同时可达 Gradio 根路径（audio-station 默认提供）。

---

## 六、 开源组件与许可

本项目代码以 MIT 协议发布（见 [LICENSE](LICENSE)）。运行依赖的第三方组件及其许可：

| 组件 | 用途 | 上游 | 许可 |
| :--- | :--- | :--- | :--- |
| **Seed-VC** (V1, 44.1kHz SVC) | 歌声转换核心引擎 | <https://github.com/Plachtaa/seed-vc> | GPL-3.0 |
| **qqmusic-api-python** (0.8.2) | QQ 音乐搜索 / 音源接口 | <https://pypi.org/project/qqmusic-api-python/> | GPL-3.0-or-later |
| **AstrBot** | 机器人框架 | <https://github.com/AstrBotDevs/AstrBot> | AGPL-3.0 |
| gradio-client / aiohttp / niquests | 进度流 / HTTP 客户端 | PyPI | Apache-2.0 / Apache-2.0+MIT / Apache-2.0 |

> 注：Seed-VC 与 qqmusic-api-python 均为 **GPL-3.0+** 依赖。本插件源码以 MIT 发布，与 GPL 依赖以「调用独立进程/独立包」方式协作（插件仅通过 HTTP/Gradio API 与 Seed-VC 网关交互，不链接其代码、不随插件分发其源码），不产生 GPL 传染。

配套后端 audio-station 额外使用的模型权重（尤其 **Breeze TTS 2 权重为非商用许可**，源码为 Apache-2.0）在 audio-station 仓库的「开源组件与许可」一节中列出。

使用本项目即表示你已理解并自行遵守上述各上游组件的许可条款。

---

## 七、 常见故障排查

### 点唱直接失败：`AsyncSession.__init__() got an unexpected keyword argument 'allow_incoming_cookies'`

**原因**：`qqmusic-api-python 0.8.2` 在构造 HTTP 会话时会传 `allow_incoming_cookies` 参数（`transport.py:354`），该参数自 **niquests 3.19.0** 才新增；环境里若装着 3.18.x 等旧版，`QMClient()` 一构造就抛 TypeError，点唱在「搜索歌曲」阶段即失败。

**修复（推荐，一处命令）**——在 AstrBot 所在环境/容器内升级：

```bash
pip install -U "niquests[speedups]>=3.19"
# Docker 部署：
docker exec -it <AstrBot容器名> pip install -U "niquests[speedups]>=3.19"
docker restart <AstrBot容器名>
```

**兜底**：v1.10.2 起插件内置 `_patch_niquests_compat()`（v1.10.1 的兼容层因幂等判断误用 `getattr` 继承了父类标记，只补到 `Session` 而漏掉真正报错的 `AsyncSession`，请勿使用 v1.10.1），检测到旧版 niquests 时会自动吞掉该参数（旧版默认行为本就是接收响应 Cookie，语义等价），因此**只要同步了最新 `main.py`，不升级依赖也能用**；启动日志会打印 `niquests x.y.z 缺少 allow_incoming_cookies，已为 Session/AsyncSession 注入兼容层`。

---

## 八、 免责声明

- 本插件的音乐搜索/下载能力依赖第三方音乐平台接口（含 QMCv2 本地解密），仅供个人学习研究，请勿用于商业用途；请支持正版音乐。
- 翻唱产物涉及原曲词曲著作权与表演者权利，公开发布前请自行取得相应授权。
- 使用本项目产生的一切后果由使用者自行承担。
