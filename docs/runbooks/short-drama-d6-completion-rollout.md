# 短剧 D-6 完成确认发布、迁移与回滚

## 发布原则

`HQ_SHORT_DRAMA_COMPLETION_ENABLED` 默认必须为 `0`。关闭时，已有
`assembly_review -> completed` 和 `/assembly/confirm` 兼容路径继续可用；只有历史
`completed` 项目全部迁移或人工复核完毕后，才能把开关设为 `1`。

所有命令都应针对数据库备份副本先演练。不得在没有备份、没有 run ID 或仍存在
`manual_review` 项目时开启 D-6。

## 发布顺序

1. 停止发布写入窗口并备份 SQLite 数据库，同时记录备份文件的 SHA-256。
2. 部署兼容版本，保持 `HQ_SHORT_DRAMA_COMPLETION_ENABLED=0`。
3. 启动一次应用以幂等创建 D-6 表和迁移批次表，然后保持服务开关关闭。
4. 执行只读审计：

   ```bash
   python scripts/migrate_short_drama_legacy_completions.py \
     --db /path/content.db --limit 1000 \
     --manual-review-out /secure/review/pr887-dry-run.json
   ```

5. 处理输出中的 `manual_review`。常见原因及动作：

   - `legacy_final_attempt_missing`：核对原正式导出任务、扣点与归档记录；证据不足时不得补造快照。
   - `legacy_final_asset_invalid`：重新归档可信正式资产并复核尺寸、时长、SHA-256 和 owner。
   - `legacy_active_job`：等待任务结束，或按任务恢复手册完成终止/退款后重跑审计。
   - `legacy_billing_unsettled`：完成扣点或退款对账后重跑审计。
   - `legacy_snapshot_pointer_mismatch`：人工核对项目指针和已有快照，禁止自动覆盖。

6. 选择唯一发布批次号并执行迁移：

   ```bash
   RUN_ID=short-drama-d6-YYYYMMDD-HHMM
   python scripts/migrate_short_drama_legacy_completions.py \
     --db /path/content.db --limit 1000 --apply --run-id "$RUN_ID" \
     --manual-review-out /secure/review/$RUN_ID.json
   ```

   相同 `run-id` 和相同数据库重复执行只会返回原报告，不会创建第二份快照。

7. 执行批次验证和全库完整性检查：

   ```bash
   python scripts/migrate_short_drama_legacy_completions.py \
     --db /path/content.db --verify --run-id "$RUN_ID"
   python scripts/check_short_drama_completion_integrity.py \
     --db /path/content.db --stale-seconds 300
   ```

8. 只有两条命令都返回 0、且 manual-review 文件为空时，才灰度设置
   `HQ_SHORT_DRAMA_COMPLETION_ENABLED=1` 并重启内容服务。
9. 验证一个新项目的 readiness、原子确认、幂等重放、完成详情和永久只读；同时抽查迁移项目。

## 回滚

如果新入口出现故障，先把 `HQ_SHORT_DRAMA_COMPLETION_ENABLED` 恢复为 `0`，使旧完成
路径立即恢复。若还需要撤销该批次迁移：

```bash
python scripts/migrate_short_drama_legacy_completions.py \
  --db /path/content.db --rollback --run-id "$RUN_ID"
python scripts/check_short_drama_completion_integrity.py \
  --db /path/content.db --stale-seconds 300
```

回滚只清除该批次写入的 completion 指针和快照，保留项目原有 `completed` stage、revision、
正式资产及审计历史。若项目指针、revision 或快照在迁移后发生变化，回滚会原子失败并进入
人工复核，不会部分回滚。回滚后的项目重新成为待迁移 legacy 数据，因此完整性检查应恢复到
本批次 dry-run 前的已知问题集合，而不是被误判为 D-6 已完成状态；开关必须继续保持 `0`。
