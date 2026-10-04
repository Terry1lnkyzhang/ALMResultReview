# AI Review 当前规则说明

> 整理日期：2026-10-02。本文说明**当前工作区源码**的行为，不代表历史已保存结果一定采用同一版本。评审规则与运行环境的配置、台账、Skill 文件共同决定实际结论；有冲突时，以执行该次评审时保存的 `ReviewResult` 和流水线记录为准。
>
> 本文是规则及操作说明，不是 AI 提示词本身。阶段声明见 [review_pipeline.toml](../app/review_pipeline.toml)，实际执行见 [reviews.py](../app/services/reviews.py)；各 Skill 的正式指令见 [review_skills](../app/review_skills)。

## 1. 目标、对象和判定边界

AI Review 审查的是 **ALM 运行记录与支持该记录的证据是否一致、充分、可追溯**，不是把 ALM 测试本身的 Passed/Failed 状态重新判成通过/失败。例如，ALM Step 为 `Failed`，Actual 明确记录了与 Expected 不符的现象，而且相关证据支持这个失败结果，评审可以判该 Step 的**记录**合格；相反，ALM Step 为 `Failed`，但 Actual 或报告写的是完全成功，则属于记录不一致。

- 批量工作范围为有当前版本的 `Passed` / `Failed` Run；单条评审、ALM 同步和批量重评各有独立入队规则。新版本与已有评审结果保留为历史；**新任务排队或运行不会清空相同版本已保存的结果**。
- 输入以该 Run 的不可变 `RunRevision.snapshot_json` 为准，先规范化 Run/Step 的状态、Description、Expected、Actual、编号项、格式和证据候选；并非每次实时读取 ALM 原文。`source_hash` 标识源版本，`review_hash` 用于比较评审相关内容，二者用途不同。见 [hashing.py](../app/hashing.py)。
- 程序负责路径、文件、台账、日期、ID、引用来源等确定性核查；Skill 仅对程序授权且提供的数据做语义判断，**不能自行读取数据库、文件、网络或写入最终结论**。所有模型输出必须满足 schema、身份覆盖和专项校验。
- 最终 `ReviewResult.verdict` 只有 `qualified`、`unqualified`、`needs_manual_review`；`review_failed` 是**没有可用当前结果时的任务失败状态**，不是一种模型裁决。

## 2. 当前流水线与开关

[声明顺序](../app/review_pipeline.toml)为 `routing → text_review → location_review → image_review → report_review → equipment_review → aggregation`，共七个阶段。注意**实际调用先执行 `text_review` 再执行 `routing`**：引用角色来自首轮文本 Skill；保存的 trace 按声明顺序呈现，并不表示 routing 额外调用了模型。程序预备步骤先产生设备/证据候选，之后按以下流程执行：

| 阶段 | 调用或规则 | 条件与输出 |
| --- | --- | --- |
| 文本评审 `text_review` | `alm-text-review` v1.7.4；步骤适用性、ALM 文本语义、语言、引用角色及设备抽取 | 每次 Review 都执行；可按预算分多批；输出仅为首轮判断 |
| 路由 `routing` | 程序消费首轮返回的候选角色，生成路径/图像/HTML/设备检查请求 | 本阶段 `ai_calls=0`；ALM 图像附件强制进入相应检查 |
| 位置 `location_review` | 程序选执行时点的权威位置配置；有唯一可用配置时调用 `location-consistency` v1.0.1 | 非现场位置、缺失或歧义等先由程序处理；可能不调用模型 |
| 图像 `image_review` | 安全解析 + `image-evidence-review` v1.3.2 | Workspace 启用外部证据且存在可审图片时调用；否则 disabled / not_applicable |
| HTML `report_review` | 安全解析、发布信息硬规则 + `html-evidence-review` v1.8.1 | Workspace 启用外部证据且 Actual 引用 HTML 时审查；可能多批调用 |
| 设备 `equipment_review` | 台账确定性判断 + 未解问题用 `equipment-role` v1.4.2 | Workspace 启用设备审查时执行；明确结果无须第二轮 AI |
| 汇总 `aggregation` | 程序合并 issue、warning、criterion 和 verdict | 不调用 AI |

外部证据开关 `external_evidence_review_enabled` 控制**实际**读取 HTML、图片及对应专项 AI；禁用后，程序仍可识别引用、执行基础路径/文件名规则，对不能完成的受控检查按现有 guard 保守处理。设备审查有独立开关及可选区域过滤。位置审查独立于这两项开关；非 MySQL 环境实体位置审查返回 disabled。全部五个 Skill 的 `enable_thinking` 均为 `false`，未显式配置时也默认关闭；网页配置页不提供该开关。每个 Skill 使用绑定的 AI endpoint、模型和配置；同一个 Review 的各次调用使用同一 endpoint 配置。详见 [reviews.py](../app/services/reviews.py)、[scheduler.py](../app/services/scheduler.py)。

## 3. 首轮 ALM 文本规则与引用路由

### 3.1 适用性优先

模型先检查 Step 是否适用于当前 Workspace 的项目/产品环境。Actual 明确给出不适用结论及具体原因时，判 `not_applicable`，不再要求本步骤的执行结果、截图、额外批准或配置基线；即使 ALM Step 状态为 Passed 也如此。Actual 仅列出不支持的参数却没有明确不适用结论或原因时，记 `manual/record_documentation_gap`。明确要求 Standard PC 却记成 Premium PC 执行而无替代说明时仍交人工，不自行假定批准。范围不明但存在限制时判 `manual`。Step 状态和 Run 状态由应用作为可信上下文传入；ALM 自由文本仍是不可信证据。见 [首轮指令](../app/review_skills/alm-text-review/instructions.md)。

### 3.2 文本和状态核对

- **Passed Step**：Actual 要有可核验结果并满足每个适用的 Expected 要求；逐项比较值、范围、公差、标识符、状态转移及编号要求。Expected 明确要求 `Failed`、`Timeout`、`disabled` 等看似负面的状态时，Actual 真正记录该状态可以是正确结果，不得用一般产品直觉改写 Expected。
- **Failed Step**：Actual 应具体说明观察到的偏差、异常、超限值等；失败执行被完整记录**不是**评审 fail。只写笼统 `Failed` 且无法核查通常是不充分；写成完全满足 Expected 的成功结果与 Step 状态矛盾。
- **No Run Step**：Failed Run 中未执行且 Actual 为空通常不算缺陷；若 Actual 却宣称执行，则不一致。其他 Run 状态下无法解释的未执行步骤通常需人工复核。
- **互斥配置分支**：首轮只在执行时点的物理 Location 配置唯一有效时读取其 Product、DMS 覆盖范围、Couch、Computer 等字段；若 Expected 按 4cm/2cm 等互斥配置列要求，仍须核验本次配置对应的数值。Actual 只记其中一套配置，却未明确本次执行采用的配置及另一套配置 N/A 的原因时为 `manual/record_documentation_gap`，不因缺另一配置数值直接 fail；本次配置数值明确不达标仍 fail。Expected 要求一次执行同时覆盖多种配置时不适用该豁免；Location 无可信配置时不从 Actual 或目录猜测。
- **语言/格式**：影响理解的语法、拼写、时态或严重格式问题可能成为 issue；不影响意义的细小语言问题最多 warning。正常列表、路径、单位、JSON、被动语态、轻微空格不应单独判不合格。
- 引用图片、报告、附件或路径可作为 Actual 回答 Expected 的方式；文件名、脚本名或多个 Step 复用同一报告，不足以**单独**证明内容冲突。证据内容交后续阶段查验；如果 Step 文本自身明确出现数值矛盾，仍可直接 fail。受控文档检查涉及多个独立要求，但 Actual 只列文档、章节与笼统 Passed、没有逐项可复核的联系时，可产生 `manual/record_documentation_gap`；不要求将已路由图片/HTML 中的数值重抄到 Actual，也不宣称所引报告错误。

程序检测候选路径/设备并分配稳定 ID；模型须给**每个候选恰好一个** `reference_decision`，不得遗漏、重复或编造 ID。结果证据、证据位置和受控设备标为需要检查；无法判断用途时保守交人工。程序验证覆盖和角色/检查标记的一致性：模型修复后仍**只遗漏候选**时可补为 `uncertain + requires_check=true`，额外/重复/不合法角色不能这样补过。`not_applicable` 的执行结果 finding 被忽略，但记录说明不足的人工项保留；已转交专项证据核查的内容缺失或仅从元数据推断的冲突不再由首轮重复判定。EarthFormal02 的 Failed Step 若 Actual 引用 ADS 却没有 PD 引用，另提示人工核对偏差追踪，不把 Failed 本身判为不合格。见 [reviews.py](../app/services/reviews.py) 的 `_validate_text_references()`、`_fallback_missing_text_references()`、`_run_text_semantic_skills()`。

文本字段单项最多向首轮 AI 提供 6,000 字符，批次目标最多 24,000 字符或 15 Step；超长字段截断时会保留“已截断”提示，并给该 Step 追加 `manual`，不把局部文本视作全量通过。特别注意**一个 Review 不保证只有一次 AI 调用**，首轮可分批，输出不合规还可能额外修复。见 [reviews.py](../app/services/reviews.py) 的 `STEP_FIELD_CHAR_LIMIT` 和 `_text_skill_batches()`。

## 4. 测试位置一致性

输入为 ALM `execution_location`、Run `execution_at` 及 `folder_path` 的**最后一个父文件夹片段**。物理位置按规范化键精确找 alias：历史表的 `SY Bay17(CHESS-SCIM-0009)` 可与 ALM `CHESS-SCIM-0009` 对应，不做随意模糊匹配。程序将 ALM 执行时间按 ALM/应用时区转换后，在历史有效区间 `valid_from <= 执行时间 < valid_to` 中寻找唯一版本；不会拿现行配置冒充执行时点的历史配置。所选版本 ID、有效期、执行时间和配置快照记录到 trace。见 [location_review.py](../app/services/location_review.py)、[test_locations.py](../app/services/test_locations.py)、[timezones.py](../app/services/timezones.py)。

程序先判：

- Location 为空、无匹配位置、存在歧义、选中配置所有字段皆空或父文件夹名为空：`fail`；执行时间上有多个重叠历史版本也为 `fail`。
- 有位置但缺执行时间、历史覆盖未知或当时位置已退役：`manual`；不凭当前配置推断过去。
- `Offline`、`Laptop` 等**在全局表中启用**的非站点模式：跳过物理位置历史查找。父文件夹只是通用名称则 `not_applicable`；若明确声明产品/DMS/覆盖范围/Couch/PC 等实体配置，则 `manual/non_site_location_configuration_claim`；空父文件夹仍 `fail`。非站点名称可配置，不是对一切陌生 Location 的通用豁免。

只有程序找到 `ready` 的物理配置时才调用位置 Skill：AI 只看父文件夹最后一段，识别它**明确声明**的 Product、DMS 版本、DMS 覆盖范围、Couch、Computer；通用 `Common Config`、人名或单纯 Bay 编号不形成配置声明，返回 `not_applicable`。显式声明的每个维度须与选定配置相符；`or`、`/` 表示的备选配置有一项兼容即可。程序复核引用原文、显式标记是否遗漏、空字段不能被判匹配。确定的冲突为 fail，语义不确定为 `uncertain`；现有总评将位置的 `uncertain` **按 fail** 处理，并非 manual。见 [位置 Skill 指令](../app/review_skills/location-consistency/instructions.md) 和 [reviews.py](../app/services/reviews.py) 的 `_location_review_stage()`、`_recalculate_result()`。

## 5. 路径与外部证据安全边界

程序从 Actual 识别 UNC、盘符、file URL、HTTP URL 及文件夹/图片/HTML 候选；**受控网络证据只接受 `\\server\share\...` 格式的 UNC 路径**，拒绝 `..` 和批准根目录之外的路径。路径不可访问、未配置批准根目录或外部证据开关关闭，不能伪装成已验证。文件系统读取还逐段拒绝链接/reparse point，不通过跳转越界。批准路径不存在时，图片/HTML 只可将**已校验的同一相对路径**映射到配置的 fallback 根目录；权限拒绝、越界等不得触发任意搜索。见 [evidence.py](../app/services/evidence.py)、[image_evidence.py](../app/services/image_evidence.py)、[html_evidence.py](../app/services/html_evidence.py)。

通用路径 guard 中非 UNC 或根目录外为 `fail`，未配置根目录或功能关闭为 `manual`；解析器返回的图片 `outside_root` 常指**检测到链接/reparse point，无法证明安全边界**，其专项图片 issue 为 `manual`，两者不要混淆。`missing` 或要求图片但完全无可评审图片可判 `fail`；网络/权限不可用、来源不清或容量不足时通常为 `manual`。HTML 与图片规则见下文。

## 6. 图像证据（网络图像与 ALM 附件）

网络图像只读取支持的 PNG、JPEG、WebP/JFIF，最多递归两级、扫描 500 个目录项，单图最大 5 MiB；每 Step 最多 10 张/10 MiB，每 Run 最多 24 张/15 MiB。文件名或相对路径应能与 Step 对应；文件名开头的 `3.PNG`、`11-CPU.PNG`、`third.PNG` 等数字/序数及原有 `Step3` 均可作为步骤标记，父目录里的编号不算。多个 Step 共用无 Step 标记文件夹属于不确定映射；明确匹配的目录内图片多于可用配额时需人工复核，不会静默取前几张算作完整。ALM 图片附件最多同步每 Step 10 张，并由 ALM 指定所属 Step，无须用文件名猜测，也不走 UNC 路径校验；仍要核验 base64、MIME、签名、SHA-256、完整解码、尺寸与预算。损坏图不送 AI，留下审计信息并需人工复核；若同目录还有完好图，可送完好的图，损坏项仍保留人工问题。见 [image_evidence.py](../app/services/image_evidence.py)、[图片流程](image-evidence-review-flow.md)。

单 Step 图片传输合计不超 4 MiB 且 12 MP 时尽量原样发送；超预算在内存中尝试压缩/缩放到约 3.5 MB、10 MP、最长边 2048 px，不改源文件或附件。不能安全处理时为 `transport_too_large` → manual。模型每次最多接收同一 Step 的 4 张图片，因此 10 张分为 4/4/2 批，各批须完整覆盖自己的媒体 ID；任一批不通过或需人工复核，都会影响该 Step。图像 AI 只判断图是否支持 Description/Expected/Actual 与可信 Step 状态：清楚矛盾为 fail，不可读、不充分或不确定为 manual；Failed Step 的失败证据若支持记录，仍可 pass。分批本身不扩大每 Run 24 张的限制，也不意味着模型在同一次调用中联合查看跨批图片。

常见专项映射：`missing` / `no_images` / `no_usable_images` / `no_matching_images` → fail；`invalid_image` / `ambiguous_step_mapping` / `denied` / `unavailable` / `budget_exhausted` / `transport_too_large` / `not_checked` → manual。图片 Skill 输出两次修复仍无法通过格式或媒体覆盖校验时，**不采信 AI 输出并加 manual issue**；HTTP/传输/其他 Skill 故障不属于这个安全回退，仍可导致整个 ReviewJob 失败。见 [reviews.py](../app/services/reviews.py) 的 `_IMAGE_EVIDENCE_ISSUES`、`_image_review_stage()` 与 [图片 Skill 指令](../app/review_skills/image-evidence-review/instructions.md)。

## 7. HTML 报告与自动化发布校验

### 7.1 程序硬规则

只解析 Actual **明确引用**的 `.html`/`.htm` 文件，不扫描文件夹找“可能相关”的报告。单文件最多 5 MiB；静态抽取可见文本及 `var resultData = {...}` 的 JSON 摘要和逐项结果，不执行脚本或加载网页外部资源。HTML 报告能从已批准 UNC 根目录读取，主路径缺失时才尝试相同相对路径的 fallback；来自 fallback 的报告会被标记 `temporary_evidence_used`，标记本身不直接改变 AI verdict。见 [html_evidence.py](../app/services/html_evidence.py)。

程序对适用 Step 独立检查：

1. 相关报告应按 `base.html`、`_2.html`、`_3.html`……连续编号；缺基础文件或中间缺号 → `fail/html_sequence`。
2. 文件名中完整的 `checkcontent`、`checkstep`、`fail` 标记或 `.html` 后还有异常后缀 → `fail/html_filename`；能读取的报告即使命名失败仍可继续做内容 AI 审核。
3. HTML **文件名 stem** 须包含与 ALM Test ID 一致的完整独立 5/6 位编号；路径若存在整个目录名为 5/6 位数字的段，**每个**此类目录都必须匹配 ALM Test ID；没有数字目录不因此 fail。文件名缺号或不匹配、目录编号冲突 → `fail/html_testcase_id`。
4. 报告路径不存在 → `fail/automation_result`；权限/网络不可用、过大、无可靠可见内容或不安全链接 → `manual/automation_result`。已能读取却没有可靠 AI assessment → manual。

这些确定性失败不能被 HTML 模型的 `pass` 推翻；单 Step 可同时有命名 fail、内容 manual 和发布 fail，最终按严重度汇总。见 [evidence.py](../app/services/evidence.py)、[reviews.py](../app/services/reviews.py) 的 `_apply_capability_guards()`。

### 7.2 HTML 内容语义和引用

`html-evidence-review` 以**当前 Step 及其全部明确引用报告**为单位；同一个整案报告可以支持多个 ALM Step，但各 Step 必须找到对应段落。检查 Description 的对象/动作、Expected 的每项要求、Actual 的值与脚本身份、报告明细/汇总是否与可信 ALM Step 状态一致。Failed Step 有真实失败细节且支持 Actual 时可判 pass；报告显示全通过却将该 Step 标 Failed 是冲突。

报告 blocks 较多时，按完整 block 顺序分成约 30,000 字符预算的 evidence batches（不跳块、不把未审内容假称已审）；多批完成后再做一次 final synthesis；仅一批时直接输出 final。模型必须覆盖全部 supplied report ID，`pass` / `fail` 必须为**每份**报告给出属于该报告/块的可验证引文；程序逐行比对原始块，允许截取源行片段，不能编造引文或把同名块从另一报告挪过来。多批 final 只能复用已校验引文，缺少报告时仅能补入此前已验证的该报告引文；`pass` 还要求 Description/Expected/Actual coverage 全部 supported、结果 consistent，以及启用发布校验时发布一致性 matched。详情见 [HTML Skill 指令](../app/review_skills/html-evidence-review/instructions.md) 和 [reviews.py](../app/services/reviews.py) 的 `_html_skill_batch_inputs()`、`_validate_html_assessment()`。

输出格式或引用校验两次仍失败时，HTML 阶段使用 `manual` 安全回退，不把不可核验的模型结论当 pass；网络/HTTP 等非 invalid-output 错误不适用此回退。报告中设备代号只从**已经引用且 supports 包含 actual** 的结构化 `Actual:` 块提取明确 `Phantom Code`，留存来源后交设备阶段核查，不从任意段落挖取编号。

### 7.3 Release Table 三方校验（仅已配置项目）

如果 Workspace 配置了自动化发布项目名，且 Step 明确引用 HTML，则程序以**配置的 ProjectName + ALM Test ID** 查询 `atframeworkdb.releasetable`，按发布版本、发布时间、ID 倒序选最新记录；不会从证据路径猜项目。核验 Actual 中完整 5/6 位 Testcase ID、Actual 的 **`Test Script Name:` / `Test Scripts Name:`** 声明与全部 HTML 文件 stem 的脚本身份、HTML 路径编号，以及 Actual 声称的验证文档编号和 Rev 与 Release 行是否一致。HTML Skill 入参将这类声明称为 `Name:`，但代码并不接受单独的普通 `Name:` 标签。见 [automation_release.py](../app/services/automation_release.py)。

- Actual ID 缺失或冲突、必需脚本名声明缺失或与 HTML 文件名冲突、路径 Test ID 冲突、发布记录找不到、明确文档冲突 → 确定性 `mismatch/not_found`，Step **fail**。
- Release DB 不可用或文档信息不完整 → `unavailable/incomplete`，Step **manual**。
- HTML 与 Release 脚本名能精确/兼容匹配时为 matched；**任何未通过确定性精确/兼容比较的脚本名**在前置检查通过后可成为 `needs_ai`，不只限历史缩写。有 HTML assessment 时交 Skill 对**HTML↔Release** 做语义比对：matched → pass，mismatched → fail，uncertain/not_checked → manual；模型不能否定已确定的 ID、脚本名声明或文档冲突。**当前实现局限**：外部证据关闭、没有 HTML assessment 时，`needs_ai` 的发布子检查会落入默认 pass 分支；禁用受控证据读取仍会由路径 guard 单独产生 manual，不能将发布子检查的 pass 理解为已完成语义比对。即便 Actual 脚本名与 HTML 不一致而硬失败，HTML↔Release 的独立一致性仍可报告 matched，不能把两个不同维度的失败混为一谈。

未配置自动化发布项目时该发布核验 disabled；HTML 的内容/文件名/路径检查仍照各自条件执行。

## 8. 设备台账及校准

设备阶段只有在 Workspace 启用设备审查时才运行，可按 area 过滤台账。程序先从 Actual 提取带标签的 Equipment ID、SN、P/N、校准区间/到期日、执行日期，并查找已匹配台账设备及跨 Step 延续关系。首轮文本 Skill 可额外逐字抽取设备名、ID、SN、原文 `source_text` 和 ALM 声称的到期日；这些不是台账真值。程序逐 Step 验证原文来源与标识符，防止模型跨 Step 串号；已有确定性匹配优先。P/N/型号不当作 SN，只有符合代码条件且唯一对应台账型号时才能回退辅助匹配。见 [equipment_review.py](../app/services/equipment_review.py)。

设备角色或名称映射仍不确定时才调用 `equipment-role`，每批最多 8 Step。AI 在程序给定的候选 ID、台账名称闭集及此前确认的设备范围内判断受控设备还是 DUT/其他，是否要求本 Step 记录身份；不能自行改台账或判校准有效。**Expected/Description 中出现候选设备不等于 Actual 已记录该设备**；只有实际文本或已核验的连续使用关系才可作为匹配来源。AI 明确确认受控设备且按规则要求记录却未记录，可 `fail/equipment_missing`；在设备角色被确认后仍无法在台账匹配可靠标识，才可能 `fail/equipment_not_found`。**仅由首轮 AI 抽取的未知 ID/SN 不直接硬 fail**，而是 `manual/equipment_identifier_unrecognized`；设备角色或来源不明也走 manual。见 [equipment_pipeline.py](../app/services/equipment_pipeline.py)、[equipment_review.py](../app/services/equipment_review.py)、[设备 Skill 指令](../app/review_skills/equipment-role/instructions.md)。

实际校准有效性由程序核算：优先 Step 执行日期，缺失时用 Run 日期；台账缺校准有效期或缺执行日期一般 manual，执行日超出台账校准区间或有证据支持的 Actual 校准信息与台账冲突则 fail。对于多个设备，一个无法归属单台的到期日只有当**没有任何**已匹配设备的到期日与之吻合时才判冲突。设备当前状态是历史执行时点的弱证据：非正常状态通常只加 warning，不直接让历史执行 fail；`No calibration required` 等台账文案让**台账有效期检查和当前状态 warning**跳过。

**当前已知局限（不要将其误写成“完全跳过校准日期”）：**程序后续仍会检查从 Actual 解析出的设备日期区间，以及单台设备的声称到期日和多台设备的已知到期日集合；后两者在台账有可比较到期日时仍可能触发冲突。若免校准设备 ID 后出现测量时间戳，也可能被误当校准期并误判 `equipment_invalid`。设备日期字符串检查没有与位置历史相同的 ALM→应用时区转换。应就具体 Step 核对原文与台账。见 [equipment_review.py](../app/services/equipment_review.py) 的 `_reported_asset_ranges()` 和 `_evaluate_matches()`。

## 9. 十项检查点与最终结论

程序输出十项 criterion（名称与相关 issue 类型以 [reviews.py](../app/services/reviews.py) 的 `CRITERIA_NAMES`、`_recalculate_result()` 为准）：

| 检查点 | 关注内容 |
| --- | --- |
| `location_consistency` | 执行时点的测试位置配置与父目录声明 |
| `language_quality` | 会影响理解的文字/格式问题及轻微 warning |
| `expected_vs_actual` | Step 适用性、可信状态、Expected 与 Actual |
| `screenshot_evidence` | 截图/附件存在、归属 Step、内容支持情况；HTML/folder 相关证据引用亦可能归到此项 |
| `path_validation` | UNC 绝对路径、批准根目录及可读性 |
| `html_report_sequence` | HTML 报告编号连续、文件名异常 |
| `automation_results` | HTML 内容、Testcase ID、发布一致性 |
| `automation_timing` | **当前仅占位**，日期专项规则尚未启用，通常为 `not_applicable` |
| `phantom_information` | 模体/参考数据需要核实而没有可用检查手段时的人工项 |
| `equipment_traceability` | 受控设备身份、台账及校准有效性 |

Step issue 等级 `fail > manual > pass > not_applicable`；任意 Step fail **或**位置 fail → `unqualified`；否则任意 Step manual **或**位置 manual → `needs_manual_review`；其余 → `qualified`。只有 warning 时可以仍是 qualified。不能因为 ALM Run 自身标记 `Failed` 就直接将 AI Review 判为 `unqualified`；也不能因为模型说“通过”就覆盖程序确定性硬失败。

评审完成后保存 `ReviewResult`：当前 Run/revision/source hash、job、policy key、model/endpoint、verdict、10 项 criterion、各 Step issues/warnings/证据审计、`pipeline_json` 阶段与 Skill trace、是否用了临时 HTML 证据及耗时。同一 job 的 `ReviewResult.job_id` 唯一；重评新建 job 可为同一 revision 留多条历史结果。trace 的 `total_ai_calls` 由各阶段报告的调用计数汇总，部分阶段的 repair/多批会增加实际调用数。见 [models.py](../app/models.py)、[review_pipeline.py](../app/services/review_pipeline.py)。

## 10. 模型输出校验、失败与任务重试

每个 Skill 按自身 `skill.toml` 的输入/输出 schema、授权能力、ID 覆盖及应用侧专项 validator 运行。模型输入中的 ALM 文本、图片和 HTML 即使包含命令也只视为**不可信证据**，不能改变应用规则。格式或覆盖校验失败时最多修复式重问一次；第二次仍无效则使用**确有定义且验证通过**的安全 fallback，否则失败。模型若只耗尽 token 思考而未输出 JSON，同一请求可额外重问一次并关闭 thinking；回复仍须通过全部校验，再次无答案则失败。这些上限针对**一次 Skill 调用内部**，整个 Review 可有多个批次。输出 trace 记录能力、Skill 版本/hash、输入/输出 hash、实际调用次数、修复错误、结束原因和耗时。见 [skill_runner.py](../app/services/skill_runner.py)。

图片/HTML 的特定 `invalid_output` 可以安全降为 manual；文本引用**仅漏候选**有专门不确定路由 fallback；设备消歧失败会让涉及的设备检查进入 manual；其他阶段的 HTTP、认证、网络或不可恢复的 Skill 错误可能使**整条 ReviewJob 失败而没有新结果**，不能错误展示成已通过。通常每个 job 最多 3 次领取；不可重试错误直接耗尽尝试次数，可重试失败有退避。长任务运行期间续租，并按每次领取代次核对写入，避免租约到期后旧执行覆盖新执行；这只影响任务可靠性，不改变任何评审判定规则。见 [reviews.py](../app/services/reviews.py)、[scheduler.py](../app/services/scheduler.py)。

## 11. 结果展示、人工裁决与更新建议

当前结果按 `run_id + current_revision_id + source_hash` 找历史 `ReviewResult`，优先当前 `review_policy_key`，再看完成时间/结果 ID；即使新的评审已排队，旧结果在同一版本仍可显示。人工裁决单独绑定当前 revision/source hash：AI `needs_manual_review` 可人工确认合格/不合格，AI `unqualified` 可人工强制合格，带 warning 的 AI `qualified` 可确认警告；若完全无结果且属于 review_failed，还可由人工记录强制合格。理由必须非空，已记录人工裁决需先撤销才能另作裁决。人工确认改变**最终显示状态**，不重写 AI 原 verdict；新 ALM 版本/source hash 会使旧裁决不再是当前决定。见 [reviews.py](../app/services/reviews.py) 的 `current_review()`、`allowed_manual_decisions()`、`save_manual_decision()`。

来自临时 fallback 的 HTML 证据会保留 `temporary_evidence_used` 标记，页面要求归档后重评；**这只是提醒，不是后端禁止人工强制合格的规则**。当前人工强制合格仍可记录，并且只要 Run 的 revision/source hash 未变且该裁决未撤销，重评后仍可继续影响最终显示结论；临时证据标记本身依旧保留。一般批量重评/推荐更新会跳过当前已有人工裁决的 Run；临时证据专用批量筛选可显式允许这类 Run 重评，单条 `Review now` 也可重评。见 [review_operations.py](../app/services/review_operations.py)、[run_detail.html](../app/templates/run_detail.html)。

`review_policy_key` 纳入 Workspace、AI pool、Skill 版本与内容、位置/发布快照、设备台账及规则文件哈希；因此政策变化可显示**建议更新评审**，但不会自动删除旧结果，也不代表旧结果必然错误。当前 `REVIEW_ENGINE_VERSION` 为 `2026.10.02.1`，且 `reviews.py` 全文件参与实现哈希；**即使只调整任务续租代码，也可能触发更新建议**。比较两次历史结论时请同时看其 policy key、revision、源 hash 与 pipeline trace，而不要将当前规则反推到旧结果。见 [review_policy.py](../app/services/review_policy.py)、[review_status.py](../app/services/review_status.py)。

---

**阅读入口**：图片细节另见 [image-evidence-review-flow.md](image-evidence-review-flow.md)；正式 AI 指令在 [各 Skill 的 instructions](../app/review_skills)，确定性规则在 [services](../app/services)，当前评审阶段在 [review_pipeline.toml](../app/review_pipeline.toml)。