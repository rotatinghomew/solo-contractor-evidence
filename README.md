# 跨机构联办履约扣减与争议复算

数据来自独立设计的合成业务记录，不含真实人员资料。文件为 UTF-8 JSON。

## 资料字典

`promises` 的预约与分段共同确定已约义务，每段的原承办方和使用的合同版本不随事后出证变化。分段时刻采用当地 24 小时制，开始计入、结束不计入；对取消，双方收到都在离开始至少 `cancel_notice_hours` 时才算提前。`evidence` 的 `hour` 在履行回执是服务时刻，在取消行是该方收到时刻；`other_party_hour` 是另一方收到时刻。`transfers` 只有授权有效且在履行开始前接手才变更可核付责任。`claims` 的审批日空值是仍未确认；`alongside` 只指关联证据，不能替代本段参与确认。`ledger` 是经过历史核定的付款或审核行，付款与看板上的计算期间不能混为同一账期。联办额外款为整场一个固定金额，由整场达到合同条件时的主办方领取（主办方为预约最早分段的原承办方）；独立段无此附加款。
`quarter_rules` 的权重用于“窗口内已完成且可归责”的加权有效义务比率；有效免责段不进入其分母。达到 `threshold` 的服务方以后一期可承接的加权义务上限为 `next_band` 中的达线档，范围跨越门槛时只允许有限档直至核实。`review_capacity` 是委员会能索取的资料次数上限，`evidence_options` 的每项可为同一个 `case` 的一个或多个分段取证；`claim_cases` 列同一争议下互斥的归属解释，`group` 相同的解释不能并选。`claim_timing` 提供发现和定案时点及凭证；已定案的案件不再作为自由解释。查询尚未裁定的候选不改变实际审批或回款。
`claims` 中的 `supersedes` 指经审批替换的旧裁定，`accepted_hour` 是新证明确认的实际承接时刻，并非审批时间。日序是入账与复核登记顺序，场次时刻单独记录。
`correction_periods` 中 `released_ceiling` 为当期新差额释放额度；`returned` 是实际收到的冲回凭证及金额，可增加可释放余额，不是审批的冲回计划。额度不足时先处理审批日更早的无争议更正，审批日相同按复核号；同一笔授权可分期释放，但每次须注明原获批款项和未消耗余额。

## 读取与核对

使用 Python 3.11。
资料读取只需标准库。

用 `python3 -m json.tool data/records.json` 查看原始记录。用 `python3 -m unittest discover -s tests -v` 核对演练资料的格式及关联。

## 业务复算实现（新增）

仓库在原输入核对之外，提供仅依赖标准库的同一套结算/复核引擎：

```bash
python3 solve.py        # 生成 outputs/result.json、outputs/headline.json、outputs/report.md
python3 -m unittest discover -s tests -v
```

- `settlement.py`：从同一份 `records.json` 计算分段状态（done/disputed/failed/missing/exempt）、真正承接与责任方、单段费与联办整场附加款、场地/责任方/预约月守恒汇总、台账逐行差异、冲回与分期释放排程、季度加权比率及情景区间、调证优先级和经办人分流。
- `report.py`：渲染 `outputs/report.md`（中文复核报告）。
- `outputs/headline.json`：扁平关键结论；`outputs/result.json`：含全部 180 段明细的完整结果。
- `tests/test_settlement.py`：固定关键边界（双方及时取消、严格开始前承接、X2/X5 移责、释放排程、季度区间、调证顺序等）。

口径要点：取消免责以双方回执均满足提前量为准；原始 `transfers` 的 `accepted_hour=12` 等于 p1 开始 12，按“开始前”严格口径不生效，移责以已审批的 X2/X5 为准（另在结果中给出 `<=` 边界敏感性）。已付台账只做冲回/差异，不被改写。
